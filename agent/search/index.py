"""Единый гибридный индекс (ТЗ 04): SQLite FTS5 (BM25) + векторы + фильтры + вес свежести, коллекции в одной базе.

Коллекции: `facts` (факты), `episodes` (эпизоды), `self` (воспоминания о себе), `reference` (справочник) и любые другие
имена. Документ: `collection`, `key` (уникален в коллекции), `text`, `ts`, `meta` (JSON). Векторы лежат в BLOB (float32)
и сравниваются перебором: для личной памяти (десятки тысяч записей) это быстрее, чем тащить расширение `sqlite-vec`;
если корпус вырастет, векторный шаг заменяется без изменения интерфейса.

Слияние: Reciprocal Rank Fusion по двум спискам (FTS и вектор), порог релевантности, множитель коллекции, вес свежести
(полураспад в днях), замена устаревших записей (`supersedes`). Чисто синхронный код: `HybridSearch` уводит его в поток."""
import array
import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from search.normalize import fts_body, fts_query

RRF_K = 60


@dataclass
class SearchConfig:
    candidates: int = 30            # сколько берётся из каждого списка до слияния
    min_cos: float = 0.60           # кусок проходит, если cos не ниже
    min_cos_bm25: float = 0.45      # или bm25_rank <= bm25_top и cos не ниже этого
    bm25_top: int = 3
    recency_weight: float = 0.0     # 0 - свежесть не влияет; 0.2 - до 20% веса от свежести
    half_life_days: float = 30.0
    boosts: dict = field(default_factory=dict)   # множитель score по коллекции: {"experience": 1.2}


@dataclass
class Hit:
    id: int
    collection: str
    key: str
    text: str
    ts: float
    meta: dict
    score: float
    cos: float | None
    bm25_rank: int | None


def _pack(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class HybridIndex:
    def __init__(self, path: Path | str = ":memory:", clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        self._clock = clock
        with self._lock:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS docs (
                    id INTEGER PRIMARY KEY, collection TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL,
                    ts REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}', vec BLOB, invalid_at REAL,
                    UNIQUE (collection, key));
                CREATE INDEX IF NOT EXISTS docs_coll ON docs (collection, ts);
                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2');
            """)
            self._db.commit()

    # ── запись ──────────────────────────────────────────────────────────────

    def upsert(self, collection: str, key: str, text: str, *, vec: list[float] | None = None, ts: float | None = None,
               meta: dict | None = None, supersedes: list[str] | None = None) -> int:
        """Добавляет или обновляет запись. supersedes: ключи записей той же коллекции, которые это заменяет."""
        ts = self._clock() if ts is None else ts
        blob = _pack(vec) if vec is not None else None
        with self._lock:
            row = self._db.execute("SELECT id FROM docs WHERE collection=? AND key=?", (collection, key)).fetchone()
            if row:
                doc_id = row[0]
                self._db.execute("UPDATE docs SET text=?, ts=?, meta=?, vec=?, invalid_at=NULL WHERE id=?",
                                 (text, ts, json.dumps(meta or {}, ensure_ascii=False), blob, doc_id))
                self._db.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
            else:
                cur = self._db.execute("INSERT INTO docs (collection, key, text, ts, meta, vec) VALUES (?,?,?,?,?,?)",
                                       (collection, key, text, ts, json.dumps(meta or {}, ensure_ascii=False), blob))
                doc_id = cur.lastrowid
            self._db.execute("INSERT INTO docs_fts (rowid, body) VALUES (?, ?)", (doc_id, fts_body(text)))
            for old in supersedes or []:
                self._db.execute("UPDATE docs SET invalid_at=? WHERE collection=? AND key=? AND id<>?",
                                 (ts, collection, old, doc_id))
            self._db.commit()
        return doc_id

    def invalidate(self, collection: str, key: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE docs SET invalid_at=? WHERE collection=? AND key=?",
                                   (self._clock(), collection, key))
            self._db.commit()
            return cur.rowcount > 0

    def delete(self, collection: str, key: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT id FROM docs WHERE collection=? AND key=?", (collection, key)).fetchone()
            if not row:
                return False
            self._db.execute("DELETE FROM docs_fts WHERE rowid=?", (row[0],))
            self._db.execute("DELETE FROM docs WHERE id=?", (row[0],))
            self._db.commit()
            return True

    def keys(self, collection: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT key FROM docs WHERE collection=?", (collection,)).fetchall()]

    def collections(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT collection, COUNT(*) FROM docs GROUP BY collection").fetchall())

    def count(self, collection: str | None = None) -> int:
        with self._lock:
            if collection:
                return self._db.execute("SELECT COUNT(*) FROM docs WHERE collection=?", (collection,)).fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    # ── поиск ───────────────────────────────────────────────────────────────

    def _where(self, collections, since, until, meta, include_stale) -> tuple[str, list]:
        conds, args = [], []
        if collections:
            conds.append("d.collection IN (%s)" % ",".join("?" * len(collections)))
            args += list(collections)
        if since is not None:
            conds.append("d.ts >= ?")
            args.append(since)
        if until is not None:
            conds.append("d.ts <= ?")
            args.append(until)
        if not include_stale:
            conds.append("d.invalid_at IS NULL")
        return (" AND ".join(conds) or "1=1"), args

    def search(self, query: str, qvec: list[float] | None = None, *, collections: list[str] | None = None, k: int = 5,
               since: float | None = None, until: float | None = None, meta: dict | None = None,
               include_stale: bool = False, cfg: SearchConfig | None = None) -> list[Hit]:
        """meta: точное совпадение полей мета-данных записи. qvec нет (эмбеддер недоступен): только FTS, без порога cos."""
        cfg = cfg or SearchConfig()
        where, args = self._where(collections, since, until, meta, include_stale)
        match = fts_query(query)
        with self._lock:
            fts_rank: dict[int, int] = {}
            if match:
                rows = self._db.execute(
                    f"SELECT f.rowid FROM docs_fts f JOIN docs d ON d.id=f.rowid WHERE docs_fts MATCH ? AND {where} "
                    f"ORDER BY bm25(docs_fts) LIMIT ?", [match, *args, cfg.candidates * 4 if meta else cfg.candidates]).fetchall()
                fts_rank = {r[0]: i + 1 for i, r in enumerate(rows)}
            cos: dict[int, float] = {}
            vec_rank: dict[int, int] = {}
            if qvec is not None:
                rows = self._db.execute(f"SELECT d.id, d.vec FROM docs d WHERE d.vec IS NOT NULL AND {where}", args).fetchall()
                scored = sorted(((cosine(qvec, _unpack(b)), i) for i, b in rows), reverse=True)
                cos = {i: c for c, i in scored}
                vec_rank = {i: n + 1 for n, (_, i) in enumerate(scored[:cfg.candidates])}
            ids = set(fts_rank) | set(vec_rank)
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            docs = {r[0]: r for r in self._db.execute(
                f"SELECT id, collection, key, text, ts, meta FROM docs WHERE id IN ({marks})", list(ids)).fetchall()}
        now = self._clock()
        hits: list[Hit] = []
        for i in ids:
            _, coll, key, text, ts, meta_json = docs[i]
            m = json.loads(meta_json or "{}")
            if meta and any(m.get(a) != b for a, b in meta.items()):
                continue
            c = cos.get(i)
            r = fts_rank.get(i)
            if qvec is not None and c is not None:
                if not (c >= cfg.min_cos or (r is not None and r <= cfg.bm25_top and c >= cfg.min_cos_bm25)):
                    continue
            elif qvec is not None and r is None:
                continue
            score = sum(1.0 / (RRF_K + x) for x in (r, vec_rank.get(i)) if x)
            score *= cfg.boosts.get(coll, 1.0)
            if cfg.recency_weight > 0:
                decay = 0.5 ** (max(0.0, now - ts) / 86400.0 / cfg.half_life_days)
                score *= 1.0 - cfg.recency_weight + cfg.recency_weight * decay
            hits.append(Hit(i, coll, key, text, ts, m, score, c, r))
        hits.sort(key=lambda h: -h.score)
        return hits[:k]
