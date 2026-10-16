"""ReminderRepository и ThoughtJournal на JSON-файлах рядом с памятью персонажа (ТЗ 30, IMP-2, IMP-6).

`path_of` возвращает текущий путь файла: при переключении персонажа папка памяти меняется, данные перечитываются.
"""
import json
import os
import secrets
import time
from pathlib import Path
from typing import Callable

THOUGHTS_MAX = 500


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class JsonReminders:
    def __init__(self, path_of: Callable[[], Path], clock: Callable[[], float] = time.time):
        self._path_of, self._clock = path_of, clock

    def _all(self) -> list[dict]:
        data = _read(Path(self._path_of()), [])
        return data if isinstance(data, list) else []

    def add(self, due: float, text: str, *, source: dict | None = None, depth: int = 0, untrusted: bool = False,
            ttl_s: float | None = None) -> dict:
        rec = {"id": "r-" + secrets.token_hex(4), "due": float(due), "text": str(text)[:500], "source": source or {},
               "status": "pending", "depth": int(depth), "untrusted": bool(untrusted), "created": self._clock(),
               "ttl_s": ttl_s}
        items = self._all()
        items.append(rec)
        self._save(items)
        return rec

    def _save(self, items: list[dict]) -> None:
        keep = [r for r in items if r.get("status") == "pending"] + \
               [r for r in items if r.get("status") != "pending"][-200:]
        _write(Path(self._path_of()), keep)

    def get(self, reminder_id: str) -> dict | None:
        return next((r for r in self._all() if r.get("id") == reminder_id), None)

    def pending(self) -> list[dict]:
        return sorted((r for r in self._all() if r.get("status") == "pending"), key=lambda r: r["due"])

    def set_status(self, reminder_id: str, status: str) -> None:
        items = self._all()
        for r in items:
            if r.get("id") == reminder_id:
                r["status"] = status
        self._save(items)


class JsonThoughts:
    def __init__(self, path_of: Callable[[], Path]):
        self._path_of = path_of

    def append(self, thought: dict) -> None:
        path = Path(self._path_of())
        items = _read(path, [])
        items = items if isinstance(items, list) else []
        items.append(thought)
        _write(path, items[-THOUGHTS_MAX:])

    def recent(self, n: int = 50) -> list[dict]:
        items = _read(Path(self._path_of()), [])
        return list(reversed(items[-n:])) if isinstance(items, list) else []

    def update(self, thought_id: str, **fields) -> bool:
        """Поменять поля мысли (стадия краткосрочной памяти); True, если мысль нашлась."""
        path = Path(self._path_of())
        items = _read(path, [])
        found = False
        for t in items if isinstance(items, list) else []:
            if t.get("id") == thought_id:
                t.update(fields)
                found = True
        if found:
            _write(path, items)
        return found
