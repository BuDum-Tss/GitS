"""Хранилище эпизодов `memory/episodes.db` (ТЗ 27, EPI-1): SQLite в режиме WAL.

Таблицы: `episodes` (запись эпизода со статусом и версией), `episode_events` (источники: копия текста события, чтобы
эпизод читался, даже когда сырое событие удалено по сроку, EPI-10), `digests` (суточные сводки и счётчики спокойного),
`diary` (дневник от первого лица), `meta` (отметки вроде «обработано до события N»). Полнотекстовый индекс и векторы лежат
в том же файле: их ведёт `search.HybridIndex` (FTS5 и float32-векторы, ТЗ 04); отдельные таблицы здесь не нужны.

Всё синхронное: сценарии зовут методы через `asyncio.to_thread`. Метод `add_episode` идемпотентен: повторная запись того же
`id` ничего не меняет (повторная нарезка безопасна, EPI-7).
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

FILE = "episodes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY, t_start REAL NOT NULL, t_end REAL NOT NULL, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
    stream TEXT NOT NULL DEFAULT '', module TEXT NOT NULL DEFAULT '', participants TEXT NOT NULL DEFAULT '[]',
    peak_id INTEGER, salience REAL NOT NULL DEFAULT 0, emotions TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
    quotes TEXT NOT NULL DEFAULT '[]', facts TEXT NOT NULL DEFAULT '[]', related TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 0, character TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', facts_done INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0, compressed INTEGER NOT NULL DEFAULT 0, raw_deleted INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',
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

_JSON_COLS = ("participants", "emotions", "quotes", "facts", "related")
SUMMARY_COLS = ("title", "summary", "outcome", "quotes", "facts", "emotions", "participants")


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
            self._db.commit()

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
            self._db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    # ── эпизоды ──────────────────────────────────────────────────────────────

    def add_episode(self, ep: dict, events: list[dict], *, upto: int | None = None) -> bool:
        """Новый эпизод со списком источников; если такой `id` есть, ничего не меняет (False). `upto` - новая отметка
        «обработано до события N» (пишется в той же транзакции, чтобы не разойтись с эпизодами)."""
        now = self._clock()
        with self._lock:
            try:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO episodes (id, t_start, t_end, first_id, last_id, stream, module, participants, peak_id,"
                    " salience, emotions, status, character, day, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ep["id"], ep["t_start"], ep["t_end"], ep["first_id"], ep["last_id"], ep.get("stream", ""),
                     ep.get("module", ""), json.dumps(ep.get("participants", []), ensure_ascii=False), ep.get("peak_id"),
                     float(ep.get("salience", 0.0)), json.dumps(ep.get("emotions", []), ensure_ascii=False),
                     ep.get("status", "pending"), ep.get("character", ""), ep.get("day", ""), now, now))
                added = cur.rowcount > 0
                if added:
                    self._db.executemany(
                        "INSERT OR IGNORE INTO episode_events (episode_id, event_id, ts, kind, text, salience) VALUES (?,?,?,?,?,?)",
                        [(ep["id"], e["id"], e["ts"], e["kind"], e["text"][:600], float(e.get("salience", 0.0))) for e in events])
                if upto is not None:
                    self._set_meta_nolock("cut_upto", upto)
                self._db.commit()
                return added
            except sqlite3.Error:
                self._db.rollback()
                raise

    def _set_meta_nolock(self, key: str, value) -> None:
        self._db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(value, ensure_ascii=False)))

    def get(self, episode_id: str) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone())

    def events_of(self, episode_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT event_id AS id, ts, kind, text, salience FROM episode_events WHERE episode_id=?"
                                    " ORDER BY event_id", (episode_id,)).fetchall()
        return [dict(r) for r in rows]

    def episodes_of_event(self, event_id: int) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT episode_id FROM episode_events WHERE event_id=?", (event_id,))]

    def select(self, *, status: str | None = None, since: float = 0.0, until: float | None = None, day: str | None = None,
             limit: int = 50, offset: int = 0, oldest_first: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM episodes WHERE t_start >= ?", [since]
        if status:
            sql += " AND status=?"
            args.append(status)
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
        """Обновить поля. Смена резюме (`summary`) увеличивает версию эпизода."""
        if not fields:
            return
        cols, args = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            args.append(json.dumps(v, ensure_ascii=False) if k in _JSON_COLS else v)
        if "summary" in fields:
            cols.append("version=version+1")
        cols.append("updated=?")
        args += [self._clock(), episode_id]
        with self._lock:
            self._db.execute(f"UPDATE episodes SET {', '.join(cols)} WHERE id=?", args)
            self._db.commit()

    def counts(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM episodes GROUP BY status").fetchall()
        out = {s: 0 for s in ("pending", "summarized", "embedded", "failed")}
        out.update({r[0]: r[1] for r in rows})
        return out

    def days(self) -> dict[str, int]:
        with self._lock:
            return {r[0]: r[1] for r in self._db.execute("SELECT day, COUNT(*) FROM episodes WHERE day != '' GROUP BY day")}

    def episodes_of_day(self, day: str, *, with_summary: bool = True) -> list[dict]:
        sql = "SELECT * FROM episodes WHERE day=?" + (" AND summary != ''" if with_summary else "") + " ORDER BY t_start"
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, (day,)).fetchall()]

    # ── сводки ───────────────────────────────────────────────────────────────

    def add_calm(self, day: str, n: int, kinds: dict, first_id: int, last_id: int, *, marker: str) -> None:
        """Спокойные события сегмента: счётчик в сводку суток. `marker` (например `cut:<последний id>`) защищает от двойного учёта."""
        with self._lock:
            if self.get_meta(f"calm:{marker}"):
                return
            r = self._db.execute("SELECT calm FROM digests WHERE day=?", (day,)).fetchone()
            calm = json.loads(r[0]) if r else {}
            calm["n"] = calm.get("n", 0) + n
            by = calm.setdefault("kinds", {})
            for k, v in kinds.items():
                by[k] = by.get(k, 0) + v
            calm["first_id"] = min(calm.get("first_id", first_id), first_id)
            calm["last_id"] = max(calm.get("last_id", last_id), last_id)
            self._db.execute("INSERT INTO digests (day, calm, updated) VALUES (?,?,?) ON CONFLICT(day) DO UPDATE SET"
                             " calm=excluded.calm, updated=excluded.updated", (day, json.dumps(calm), self._clock()))
            self._set_meta_nolock(f"calm:{marker}", 1)
            self._db.commit()

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
