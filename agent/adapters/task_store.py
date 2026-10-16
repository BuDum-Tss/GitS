"""TaskRepository на SQLite `memory/tasks.db` рядом с памятью персонажа (ТЗ 32, WS-8).

Одна таблица `tasks` (дерево по `parent`), поля `steps`, `checkpoint` и `meta` лежат JSON-текстом; таблица `meta` держит отметки
вроде «reminders.json перенесён». `path_of` возвращает текущий путь: при переключении персонажа папка памяти меняется, и файл
открывается заново. Методы блокирующие: сценарии зовут их через `asyncio.to_thread`.
"""
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable

FILE = "tasks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, workspace TEXT NOT NULL, goal TEXT NOT NULL, parent TEXT, steps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', origin TEXT NOT NULL DEFAULT 'user', priority INTEGER NOT NULL DEFAULT 3,
    due REAL, created REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, checkpoint TEXT, kind TEXT NOT NULL DEFAULT 'task',
    meta TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS t_ws ON tasks (workspace, status);
CREATE INDEX IF NOT EXISTS t_due ON tasks (due);
CREATE INDEX IF NOT EXISTS t_parent ON tasks (parent);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_COLS = ("id", "workspace", "goal", "parent", "steps", "status", "origin", "priority", "due", "created", "updated", "checkpoint",
         "kind", "meta")
KEEP_CLOSED = 300            # закрытых задач хранится не больше (старые удаляются при записи)


def _loads(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) or default is None else default


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["steps"] = _loads(d.get("steps"), [])
    d["checkpoint"] = _loads(d.get("checkpoint"), None)
    d["meta"] = _loads(d.get("meta"), {})
    return d


class TaskStore:
    def __init__(self, path_of: Callable[[], Path | str] | Path | str = ":memory:",
                 on_open: Callable[["TaskStore", Path], None] | None = None):
        """on_open(store, путь) вызывается после открытия очередной базы (миграции из старых файлов)."""
        self._path_of = path_of if callable(path_of) else (lambda: path_of)
        self._on_open = on_open
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        self._open_path: str | None = None

    def _conn(self) -> sqlite3.Connection:
        path = str(self._path_of())
        if self._db is None or path != self._open_path:
            if self._db is not None:
                self._db.close()
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
            self._db.row_factory = sqlite3.Row
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
            self._open_path = path
            if self._on_open is not None and path != ":memory:":
                try:
                    self._on_open(self, Path(path).parent)
                except Exception as e:                    # перенос не должен ломать задачи
                    logging.getLogger("tasks").warning(f"Перенос старых данных в задачи: {e}")
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ── TaskRepository ───────────────────────────────────────────────────────

    def save(self, task: dict) -> None:
        vals = (task["id"], task.get("workspace") or "assistant", task.get("goal") or "", task.get("parent"),
                json.dumps(task.get("steps") or [], ensure_ascii=False), task.get("status") or "pending",
                task.get("origin") or "user", int(task.get("priority") or 3), task.get("due"), float(task.get("created") or 0),
                float(task.get("updated") or 0), json.dumps(task["checkpoint"], ensure_ascii=False) if task.get("checkpoint") else None,
                task.get("kind") or "task", json.dumps(task.get("meta") or {}, ensure_ascii=False))
        with self._lock:
            db = self._conn()
            db.execute(f"INSERT OR REPLACE INTO tasks ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})", vals)
            self._trim(db)
            db.commit()

    def _trim(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM tasks WHERE status IN ('done','failed','cancelled') AND id NOT IN "
                   "(SELECT id FROM tasks WHERE status IN ('done','failed','cancelled') ORDER BY updated DESC LIMIT ?)", (KEEP_CLOSED,))

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            r = self._conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _row(r) if r is not None else None

    def list(self, *, workspace: str | None = None, statuses: tuple[str, ...] | None = None, kind: str | None = None,
             parent: str | None = None) -> list[dict]:
        where, args = [], []
        if workspace is not None:
            where.append("workspace=?")
            args.append(workspace)
        if statuses:
            where.append(f"status IN ({', '.join('?' * len(statuses))})")
            args += list(statuses)
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        if parent is not None:
            where.append("parent=?")
            args.append(parent)
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created, id"
        with self._lock:
            return [_row(r) for r in self._conn().execute(sql, args).fetchall()]

    def delete(self, task_id: str) -> None:
        with self._lock:
            db = self._conn()
            db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            db.commit()

    # ── отметки ──────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default=None):
        with self._lock:
            r = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default

    def set_meta(self, key: str, value) -> None:
        with self._lock:
            db = self._conn()
            db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))
            db.commit()
