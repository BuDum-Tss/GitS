"""Хранилище уроков и убеждений (ТЗ 28): SQLite `lessons.db` в папке памяти персонажа.

Таблицы: `lessons` (уроки, LRN-3), `mistakes` (разобранные ошибки: основа для доли повторов, LRN-5), `beliefs` (убеждения),
`belief_log` (журнал изменений убеждений с возможностью отмены, LRN-6), `meta`. Тексты уроков для поиска по смыслу лежат в общем
гибридном индексе (коллекция `<область>.experience`, ТЗ 04), здесь только записи и счётчики. Блокирующий код: из сценариев
вызывается через `asyncio.to_thread`."""
import json
import sqlite3
import threading
import time
from pathlib import Path

FILE = "lessons.db"
_JSON = {"episodes", "mistakes", "lessons"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, mtype TEXT NOT NULL DEFAULT '',
    situation TEXT NOT NULL, action TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '', better TEXT NOT NULL,
    confidence REAL NOT NULL, confirmations INTEGER NOT NULL DEFAULT 1, episodes TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active', created REAL NOT NULL, last_confirmed REAL, last_applied REAL,
    applied INTEGER NOT NULL DEFAULT 0, repeats_after_apply INTEGER NOT NULL DEFAULT 0, vetoed INTEGER NOT NULL DEFAULT 0,
    belief_id INTEGER, expired_reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'sleep');
CREATE INDEX IF NOT EXISTS lessons_status ON lessons (status, scope);
CREATE TABLE IF NOT EXISTS mistakes (
    event_id INTEGER PRIMARY KEY, ts REAL NOT NULL, mtype TEXT NOT NULL, scope TEXT NOT NULL, lesson_id INTEGER,
    repeat INTEGER NOT NULL DEFAULT 0, skipped TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS beliefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, lessons TEXT NOT NULL DEFAULT '[]',
    episodes TEXT NOT NULL DEFAULT '[]', confirmations INTEGER NOT NULL DEFAULT 0, ts REAL NOT NULL, previous TEXT,
    status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS belief_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL, belief_id INTEGER, text TEXT NOT NULL DEFAULT '',
    previous TEXT, reason TEXT NOT NULL DEFAULT '', lessons TEXT NOT NULL DEFAULT '[]', episodes TEXT NOT NULL DEFAULT '[]',
    who TEXT NOT NULL DEFAULT 'agent', reverted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _row(r) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in _JSON & set(out):
        out[k] = json.loads(out[k] or "[]")
    return out


class LessonStore:
    def __init__(self, path: Path | str = ":memory:", clock=time.time):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── служебное ───────────────────────────────────────────────────────────

    def get_meta(self, key: str, default=None):
        with self._lock:
            r = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    # ── уроки ───────────────────────────────────────────────────────────────

    def add_lesson(self, lesson: dict) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO lessons (scope, mtype, situation, action, outcome, better, confidence, confirmations, episodes, status,"
                " created, last_confirmed, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lesson.get("scope", "core"), lesson.get("mtype", ""), lesson["situation"], lesson.get("action", ""),
                 lesson.get("outcome", ""), lesson["better"], lesson["confidence"], lesson.get("confirmations", 1),
                 json.dumps(sorted(lesson.get("episodes", []))), lesson.get("status", "active"), self.clock(), self.clock(),
                 lesson.get("source", "sleep")))
            self._db.commit()
            return int(cur.lastrowid)

    def lesson(self, lesson_id: int) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM lessons WHERE id=?", (int(lesson_id),)).fetchone())

    def lessons(self, status: str | None = None, scope: str | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM lessons WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if scope:
            sql += " AND scope=?"
            args.append(scope)
        sql += " ORDER BY confirmations DESC, created DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def update_lesson(self, lesson_id: int, **fields) -> None:
        if not fields:
            return
        cols = []
        for k, v in fields.items():
            cols.append(f"{k}=?")
        args = [json.dumps(sorted(v)) if k in _JSON else v for k, v in fields.items()] + [int(lesson_id)]
        with self._lock:
            self._db.execute(f"UPDATE lessons SET {', '.join(cols)} WHERE id=?", args)
            self._db.commit()

    def scopes(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT scope FROM lessons WHERE status='active'").fetchall()]

    def mark_applied(self, lesson_ids: list[int]) -> None:
        if not lesson_ids:
            return
        now = self.clock()
        with self._lock:
            for i in lesson_ids:
                self._db.execute("UPDATE lessons SET applied=applied+1, last_applied=? WHERE id=?", (now, int(i)))
            self._db.commit()

    def count_lessons(self) -> dict:
        with self._lock:
            return {r[0]: r[1] for r in self._db.execute("SELECT status, COUNT(*) FROM lessons GROUP BY status").fetchall()}

    # ── ошибки ──────────────────────────────────────────────────────────────

    def add_mistake(self, event_id: int, ts: float, mtype: str, scope: str, lesson_id: int | None, repeat: bool = False,
                    skipped: str = "") -> bool:
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO mistakes (event_id, ts, mtype, scope, lesson_id, repeat, skipped)"
                                   " VALUES (?,?,?,?,?,?,?)", (int(event_id), ts, mtype, scope, lesson_id, int(repeat), skipped))
            self._db.commit()
            return cur.rowcount > 0

    def seen(self, event_id: int) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM mistakes WHERE event_id=?", (int(event_id),)).fetchone() is not None

    def mistake_rows(self) -> list[tuple]:
        """(вид, повтор после применения) для долей повторов; пропущенные (поправки фактов) не считаются."""
        with self._lock:
            return [(r[0], bool(r[1])) for r in self._db.execute("SELECT mtype, repeat FROM mistakes WHERE skipped=''").fetchall()]

    def skipped_count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM mistakes WHERE skipped<>''").fetchone()[0])

    # ── убеждения ───────────────────────────────────────────────────────────

    def add_belief(self, text: str, lessons: list, episodes: list, confirmations: int, previous: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute("INSERT INTO beliefs (text, lessons, episodes, confirmations, ts, previous) VALUES (?,?,?,?,?,?)",
                                   (text, json.dumps(lessons), json.dumps(sorted(episodes)), confirmations, self.clock(), previous))
            self._db.commit()
            return int(cur.lastrowid)

    def belief(self, belief_id: int) -> dict | None:
        with self._lock:
            return _row(self._db.execute("SELECT * FROM beliefs WHERE id=?", (int(belief_id),)).fetchone())

    def beliefs(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM beliefs", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        with self._lock:
            return [_row(r) for r in self._db.execute(sql + " ORDER BY confirmations DESC, ts DESC", args).fetchall()]

    def update_belief(self, belief_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        args = [json.dumps(v) if isinstance(v, list) else v for v in fields.values()] + [int(belief_id)]
        with self._lock:
            self._db.execute(f"UPDATE beliefs SET {cols} WHERE id=?", args)
            self._db.commit()

    # ── журнал изменений ────────────────────────────────────────────────────

    def log_change(self, action: str, belief_id: int | None, text: str, *, previous: str | None = None, reason: str = "",
                   lessons: list | None = None, episodes: list | None = None, who: str = "agent") -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO belief_log (ts, action, belief_id, text, previous, reason, lessons, episodes, who) VALUES (?,?,?,?,?,?,?,?,?)",
                (self.clock(), action, belief_id, text, previous, reason, json.dumps(lessons or []),
                 json.dumps(sorted(episodes or [])), who))
            self._db.commit()
            return int(cur.lastrowid)

    def change(self, change_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM belief_log WHERE id=?", (int(change_id),)).fetchone()
        return _row(r)

    def changes(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [_row(r) for r in self._db.execute("SELECT * FROM belief_log ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]

    def mark_reverted(self, change_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE belief_log SET reverted=1 WHERE id=?", (int(change_id),))
            self._db.commit()

    def change_times(self, actions: tuple = ("accepted", "updated")) -> list[float]:
        marks = ",".join("?" * len(actions))
        with self._lock:
            return [r[0] for r in self._db.execute(
                f"SELECT ts FROM belief_log WHERE who='agent' AND action IN ({marks})", actions).fetchall()]
