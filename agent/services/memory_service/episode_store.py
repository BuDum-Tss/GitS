"""Хранилище эпизодов `memory/episodes.db` (ТЗ 27, EPI-1): SQLite в режиме WAL. Это единая память персонажа.

Эпизод это `id`, `kind`, `title` (короткая метка для списков), `text` (свободный текст от первого лица), время, яркость
(`salience`), источники и статус. Вид `kind`:
  life   событие из журнала (нарезается из журнала, пишется форком её сессии или запасным писателем);
  canon  воспоминание персонажа о себе из memories.yaml (`mem_id`, `mtype`, `weight`); защищено: не удаляется и не перезаписывается
         ни инструментами забывания и записи, ни разговором, только загрузкой из yaml (`upsert_canon`, `remove_canon`);
  note   запись «запомни»: то, что она или владелец пожелали сохранить, договорённости и поправки, а также знания, перенесённые из старого графа.
Таблицы: `episodes`, `episode_events` (источники: копия текста события, чтобы эпизод читался, когда сырое удалено по сроку, EPI-10),
`digests` (суточные сводки), `diary` (дневник от первого лица), `meta` (отметки вроде «обработано до события N»). Полнотекстовый индекс
и векторы лежат в том же файле: их ведёт `search.HybridIndex` (FTS5 и float32-векторы, ТЗ 04); индексируется `title. text`.

Всё синхронное: сценарии зовут методы через `asyncio.to_thread`. Метод `add_episode` идемпотентен: повторная запись того же
`id` ничего не меняет (повторная нарезка безопасна, EPI-7).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

from services.memory_service.episodes import CANON, LIFE, NOTE

FILE = "episodes.db"
KINDS = (LIFE, CANON, NOTE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'life',
    t_start REAL NOT NULL, t_end REAL NOT NULL, first_id INTEGER NOT NULL DEFAULT 0, last_id INTEGER NOT NULL DEFAULT 0,
    stream TEXT NOT NULL DEFAULT '', module TEXT NOT NULL DEFAULT '', participants TEXT NOT NULL DEFAULT '[]',
    peak_id INTEGER, salience REAL NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', related TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 0, character TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    notified INTEGER NOT NULL DEFAULT 0, compressed INTEGER NOT NULL DEFAULT 0, raw_deleted INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',
    mem_id TEXT NOT NULL DEFAULT '', mtype TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ep_status ON episodes (status, t_start);
CREATE INDEX IF NOT EXISTS ep_day ON episodes (day);
CREATE TABLE IF NOT EXISTS episode_events (
    episode_id TEXT NOT NULL, event_id INTEGER NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    salience REAL NOT NULL DEFAULT 0, PRIMARY KEY (episode_id, event_id));
CREATE INDEX IF NOT EXISTS ee_event ON episode_events (event_id);
CREATE TABLE IF NOT EXISTS digests (
    day TEXT PRIMARY KEY, text TEXT NOT NULL DEFAULT '', calm TEXT NOT NULL DEFAULT '{}', episodes INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0, compressed INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS diary (
    day TEXT PRIMARY KEY, text TEXT NOT NULL, character TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1,
    sources TEXT NOT NULL DEFAULT '[]', ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_JSON_COLS = ("participants", "related")
# колонки, которых нет в базах старой схемы: добавляются при открытии (автомиграция)
_ADDED = {"kind": "TEXT NOT NULL DEFAULT 'life'", "mem_id": "TEXT NOT NULL DEFAULT ''", "mtype": "TEXT NOT NULL DEFAULT ''",
          "weight": "REAL NOT NULL DEFAULT 0"}
# колонки старой схемы, которых больше нет: отдельные поля «отношение», «заметила», цитаты, факты, эмоции и исход живут внутри текста
_DROPPED = ("attitude", "noticed", "quotes", "facts", "emotions", "outcome", "facts_done")


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for c in _JSON_COLS:
        try:
            d[c] = json.loads(d.get(c) or "[]")
        except ValueError:
            d[c] = []
    return d


def _loads(raw) -> list:
    try:
        v = json.loads(raw or "[]")
    except ValueError:
        return []
    return v if isinstance(v, list) else []


class EpisodeStore:
    def __init__(self, path: Path | str = ":memory:", clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._migrate()
            self._db.execute("CREATE INDEX IF NOT EXISTS ep_kind ON episodes (kind, mem_id)")
            self._db.commit()

    def _columns(self) -> set[str]:
        return {r[1] for r in self._db.execute("PRAGMA table_info(episodes)")}

    def _migrate(self) -> None:
        """База старой схемы -> текущая: колонки kind, mem_id, mtype, weight; рассказ `summary`, отношение и заметки сливаются в
        один `text`; отдельные колонки старой схемы убираются (если эта версия SQLite не умеет DROP COLUMN, они молча остаются неиспользуемыми)."""
        have = self._columns()
        for col, decl in _ADDED.items():
            if col not in have:
                self._db.execute(f"ALTER TABLE episodes ADD COLUMN {col} {decl}")
        if "summary" in have:
            if "text" in have:                                    # обе колонки: чужая промежуточная схема, summary не нужна
                self._db.execute("UPDATE episodes SET text=summary WHERE text='' AND summary!=''")
                self._drop("summary")
            else:
                if "attitude" in have or "noticed" in have:
                    cols = [c for c in ("summary", "attitude", "noticed") if c in have]
                    for r in self._db.execute(f"SELECT id, {', '.join(cols)} FROM episodes").fetchall():
                        extra = r["attitude"] if "attitude" in cols else ""
                        notes = "; ".join(str(x) for x in _loads(r["noticed"])) if "noticed" in cols else ""
                        merged = " ".join(p for p in (r["summary"], notes, extra) if p)
                        if merged != r["summary"]:
                            self._db.execute("UPDATE episodes SET summary=? WHERE id=?", (merged, r["id"]))
                self._db.execute("ALTER TABLE episodes RENAME COLUMN summary TO text")
        for col in _DROPPED:
            if col in self._columns():
                self._drop(col)

    def _drop(self, col: str) -> None:
        try:
            self._db.execute(f"ALTER TABLE episodes DROP COLUMN {col}")
        except sqlite3.OperationalError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── отметки ──────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default=None):
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._set_meta_nolock(key, value)
            self._db.commit()

    # ── эпизоды ──────────────────────────────────────────────────────────────

    def add_episode(self, ep: dict, events: list[dict], *, upto: int | None = None) -> bool:
        """Новый эпизод жизни со списком источников; если такой `id` есть, ничего не меняет (False). `upto` - новая отметка
        «обработано до события N» (пишется в той же транзакции, чтобы не разойтись с эпизодами)."""
        now = self._clock()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO episodes (id, kind, t_start, t_end, first_id, last_id, stream, module, participants, peak_id,"
                    " salience, status, character, day, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ep["id"], LIFE, ep["t_start"], ep["t_end"], ep["first_id"], ep["last_id"], ep.get("stream", ""),
                     ep.get("module", ""), json.dumps(ep.get("participants", []), ensure_ascii=False), ep.get("peak_id"),
                     float(ep.get("salience", 0.0)), ep.get("status", "pending"), ep.get("character", ""), ep.get("day", ""), now, now))
                added = cur.rowcount > 0
                if added:
                    self._db.executemany(
                        "INSERT OR IGNORE INTO episode_events (episode_id, event_id, ts, kind, text, salience) VALUES (?,?,?,?,?,?)",
                        [(ep["id"], e["id"], e["ts"], e["kind"], e["text"][:600], float(e.get("salience", 0.0))) for e in events])
                if upto is not None:
                    self._set_meta_nolock("moments_upto", upto)
                self._db.commit()
                return added
            except sqlite3.Error:
                self._db.rollback()
                raise

    def _set_meta_nolock(self, key: str, value) -> None:
        self._db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value, ensure_ascii=False)))

    # ── канон и заметки ──────────────────────────────────────────────────────

    def add_note(self, ep_id: str, text: str, *, title: str = "", salience: float = 0.5, ts: float | None = None,
                 character: str = "") -> bool:
        """Заметка («запомни», перенесённое знание): True, если новая (тот же `id` не перезаписывается). Статус `summarized`:
        индекс по словам делает вызывающий сразу, вектор добавит сон, когда эмбеддер доступен."""
        return self._add_memory(NOTE, ep_id, text, title=title, salience=salience, ts=ts, character=character)

    def _add_memory(self, kind: str, ep_id: str, text: str, *, title: str = "", salience: float = 0.5, ts: float | None = None,
                    mem_id: str = "", mtype: str = "", weight: float = 0.0, character: str = "") -> bool:
        now = self._clock()
        ts = float(ts) if ts else now
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO episodes (id, kind, t_start, t_end, first_id, last_id, salience, title, text, status, notified,"
                " character, mem_id, mtype, weight, created, updated) VALUES (?,?,?,?,0,0,?,?,?,?,?,?,?,?,?,?,?)",
                (ep_id, kind, ts, ts, max(0.0, min(1.0, float(salience))), title[:160], text, "summarized", 1, character,
                 mem_id, mtype, float(weight), now, now))
            self._db.commit()
            return cur.rowcount > 0

    def upsert_canon(self, mem_id: str, text: str, *, mtype: str, weight: float, character: str = "") -> str:
        """Воспоминание персонажа из yaml: `added`, `updated` или `same`. Изменённый текст возвращает статус `summarized`
        (вектор пересчитает сон). Только загрузка из yaml меняет канон."""
        ep_id = canon_id(mem_id)
        now = self._clock()
        with self._lock:
            old = self._db.execute("SELECT text, mtype, weight FROM episodes WHERE id=? AND kind=?", (ep_id, CANON)).fetchone()
            if old is None:
                self._add_memory(CANON, ep_id, text, salience=weight, mem_id=mem_id, mtype=mtype, weight=weight, character=character)
                return "added"
            if old["text"] == text:
                if old["mtype"] != mtype or float(old["weight"]) != float(weight):
                    self._db.execute("UPDATE episodes SET mtype=?, weight=?, salience=?, updated=? WHERE id=?",
                                     (mtype, float(weight), float(weight), now, ep_id))
                    self._db.commit()
                return "same"
            self._db.execute("UPDATE episodes SET text=?, mtype=?, weight=?, salience=?, status='summarized', version=version+1,"
                             " updated=? WHERE id=?", (text, mtype, float(weight), float(weight), now, ep_id))
            self._db.commit()
            return "updated"

    def remove_canon(self, mem_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM episodes WHERE id=? AND kind=?", (canon_id(mem_id), CANON))
            self._db.commit()
            return cur.rowcount > 0

    def canon_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT mem_id FROM episodes WHERE kind=?", (CANON,))}

    def of_kind(self, kind: str, *, limit: int = 500) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute("SELECT * FROM episodes WHERE kind=? ORDER BY t_start DESC LIMIT ?",
                                                      (kind, int(limit))).fetchall()]

    def delete(self, episode_id: str) -> bool:
        """Удалить эпизод жизни или заметку вместе с источниками. Канон не удаляется (False)."""
        with self._lock:
            cur = self._db.execute("DELETE FROM episodes WHERE id=? AND kind!=?", (episode_id, CANON))
            if cur.rowcount:
                self._db.execute("DELETE FROM episode_events WHERE episode_id=?", (episode_id,))
            self._db.commit()
            return cur.rowcount > 0

    def forgettable(self) -> list[dict]:
        """Эпизоды и заметки, которые можно забыть (не канон): [{id, title, text}]."""
        with self._lock:
            rows = self._db.execute("SELECT id, title, text FROM episodes WHERE kind!=? AND text!=''", (CANON,)).fetchall()
        return [dict(r) for r in rows]

    # ── чтение ───────────────────────────────────────────────────────────────

    def has_peak(self, event_ids: list[int]) -> bool:
        """Есть ли уже эпизод с одним из этих событий-пиков (момент не дублируется, даже если контекст вырос)."""
        if not event_ids:
            return False
        with self._lock:
            q = f"SELECT 1 FROM episodes WHERE peak_id IN ({','.join('?' * len(event_ids))}) LIMIT 1"
            return self._db.execute(q, [int(i) for i in event_ids]).fetchone() is not None

    def get(self, episode_id: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())

    def get_many(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM episodes WHERE id IN ({','.join('?' * len(ids))})", list(ids)).fetchall()
        return {r["id"]: _row(r) for r in rows}

    def events_of(self, episode_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT event_id AS id, ts, kind, text, salience FROM episode_events WHERE episode_id=?"
                                    " ORDER BY event_id", (episode_id,)).fetchall()
        return [dict(r) for r in rows]

    def episodes_of_event(self, event_id: int) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT episode_id FROM episode_events WHERE event_id=?", (event_id,))]

    def select(self, *, status: str | None = None, since: float = 0.0, until: float | None = None, day: str | None = None,
               kind: str | None = None, limit: int = 50, offset: int = 0, oldest_first: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM episodes WHERE t_start >= ?", [since]
        if status:
            sql += " AND status=?"
            args.append(status)
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if until is not None:
            sql += " AND t_start <= ?"
            args.append(until)
        if day:
            sql += " AND day=?"
            args.append(day)
        sql += f" ORDER BY t_start {'ASC' if oldest_first else 'DESC'} LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def update(self, episode_id: str, **fields) -> None:
        """Обновить поля. Смена текста (`text`) увеличивает версию эпизода."""
        if not fields:
            return
        cols, args = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            args.append(json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v)
        if "text" in fields:
            cols.append("version=version+1")
        cols.append("updated=?")
        args += [self._clock(), episode_id]
        with self._lock:
            self._db.execute(f"UPDATE episodes SET {', '.join(cols)} WHERE id=?", args)
            self._db.commit()

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM episodes WHERE kind=? GROUP BY status", (LIFE,)).fetchall()
            kinds = self._db.execute("SELECT kind, COUNT(*) FROM episodes GROUP BY kind").fetchall()
        out = {s: 0 for s in ("pending", "summarized", "embedded", "failed", "skipped")}
        out.update({r[0]: r[1] for r in rows})
        out["kinds"] = {k: 0 for k in KINDS} | {r[0]: r[1] for r in kinds}
        return out

    def days(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._db.execute("SELECT day, COUNT(*) FROM episodes WHERE day != '' GROUP BY day")}

    def episodes_of_day(self, day: str, *, with_text: bool = True) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE day=?" + (" AND text != ''" if with_text else "") + " ORDER BY t_start"
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, (day,)).fetchall()]

    # ── сводки ───────────────────────────────────────────────────────────────

    def digest(self, day: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM digests WHERE day=?", (day,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["calm"] = json.loads(d.get("calm") or "{}")
        return d

    def digests(self, limit: int = 30) -> list[dict]:
        with self._lock:
            days = [r[0] for r in self._db.execute("SELECT day FROM digests ORDER BY day DESC LIMIT ?", (int(limit),))]
        return [self.digest(d) for d in days]

    def set_digest(self, day: str, text: str, episodes: int, *, compressed: bool = False) -> None:
        with self._lock:
            self._db.execute("INSERT INTO digests (day, text, episodes, version, compressed, updated) VALUES (?,?,?,1,?,?)"
                             " ON CONFLICT(day) DO UPDATE SET text=excluded.text, episodes=excluded.episodes,"
                             " version=digests.version+1, compressed=MAX(digests.compressed, excluded.compressed),"
                             " updated=excluded.updated", (day, text, episodes, int(compressed), self._clock()))
            self._db.commit()

    # ── дневник ──────────────────────────────────────────────────────────────

    def put_diary(self, day: str, text: str, character: str, sources: list[str]) -> None:
        with self._lock:
            self._db.execute("INSERT INTO diary (day, text, character, version, sources, ts) VALUES (?,?,?,1,?,?)"
                             " ON CONFLICT(day) DO UPDATE SET text=excluded.text, character=excluded.character,"
                             " version=diary.version+1, sources=excluded.sources, ts=excluded.ts",
                             (day, text, character, json.dumps(sources), self._clock()))
            self._db.commit()

    def diary(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM diary ORDER BY day DESC LIMIT ?", (int(limit),)).fetchall()
        return [{**dict(r), "sources": json.loads(r["sources"] or "[]")} for r in rows]

    def diary_days(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT day FROM diary")}


def canon_id(mem_id: str) -> str:
    return f"canon:{mem_id}"
