"""Журнал запусков инструментов для чата дашборда: что запущено, кем и чем кончилось.

Кто запускает (`executor`):
  fast     - быстрый контур: планировщик на локальной модели (Gemma), `Router.handle`;
  claude   - медленный контур: Claude-помощник (`claude_task`), его внутренние вызовы Read/Bash/Edit…
             приходят от модуля как `job.update` со стадией `tool:<Имя>:<номер>` (`JobTracker`);
  external - вызов «со стороны»: модуль (`core.tool.call`), дашборд, другой агент.

Кольцо в памяти процесса, на диск не пишется (в параметрах бывают пути и команды). Аргументы и результат укорочены."""
import itertools
import threading
import time
from collections import deque

RING = 200
SUMMARY_MAX = 160
RESULT_MAX = 160
EXECUTORS = ("fast", "claude", "external")

_lock = threading.Lock()
_entries: deque = deque(maxlen=RING)
_ids = itertools.count(1)


def _clip(value, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def summarize(args) -> str:
    """Короткое «ключ=значение» по аргументам вызова."""
    if not isinstance(args, dict):
        return _clip(args, SUMMARY_MAX)
    return _clip(", ".join(f"{k}={_clip(v, 60)}" for k, v in args.items()), SUMMARY_MAX)


def start(executor: str, tool: str, summary: str = "", *, ts: float | None = None) -> int:
    """Запуск начался: запись в состоянии `running`; возвращает её номер для `finish`."""
    entry = {"id": next(_ids), "ts": ts or time.time(), "executor": executor if executor in EXECUTORS else "external",
             "tool": _clip(tool, 80), "summary": _clip(summary, SUMMARY_MAX), "state": "running", "result": ""}
    with _lock:
        _entries.append(entry)
    return entry["id"]


def finish(entry_id: int, ok: bool, result: str = "") -> None:
    """Запуск закончился (успех или ошибка); если запись уже вытеснена из кольца, ничего не делает."""
    with _lock:
        for e in reversed(_entries):
            if e["id"] == entry_id:
                e["state"] = "done" if ok else "failed"
                e["result"] = _clip(result, RESULT_MAX)
                return


def note(executor: str, tool: str, summary: str = "", *, ts: float | None = None) -> int:
    """Запуск, у которого нет отдельного конца (внутренний вызов Claude): сразу `done`."""
    entry_id = start(executor, tool, summary, ts=ts)
    finish(entry_id, True)
    return entry_id


def recent(n: int = 40) -> list[dict]:
    with _lock:
        return [dict(e) for e in list(_entries)[-n:]]


def clear() -> None:
    with _lock:
        _entries.clear()
