"""Журнал входа агента для чата дашборда: какие события реально попали в контекст модели и как.

Парный к `tool_trace` (тот про действия агента, этот про его вход). Способы (`how`):
  batch          - событие в пачке, с которой запущен ход (`ResidentRuntime._launch`) или путь без v2 (`handle_event`);
  steered        - событие, долитое в уже идущий ход (`ResidentRuntime.steer`);
  interrupt_note - блок «прервано», переданный в следующий ход;
  digest         - сводка пропущенного (вытесненные и просроченные события).

Приватность: пишется только то, что и так стоит в промпте агента (текст строки события, укороченный); `data` события не пишется.
Кольцо в памяти процесса, на диск не пишется."""
import itertools
import threading
import time
from collections import deque

RING = 200
TEXT_MAX = 400
HOWS = ("batch", "steered", "interrupt_note", "digest")

_lock = threading.Lock()
_entries: deque = deque(maxlen=RING)
_ids = itertools.count(1)


def _clip(value, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def add(source: str, kind: str, text: str, priority: float | None = None, how: str = "batch", *,
        turn: int | None = None, ts: float | None = None) -> int:
    """Записать вход агента; возвращает номер записи. source: окружение события, kind: его вид (без префикса окружения)."""
    entry = {"id": next(_ids), "ts": ts or time.time(), "source": _clip(source, 40), "kind": _clip(kind, 80),
             "priority": priority, "text": _clip(text, TEXT_MAX), "how": how if how in HOWS else "batch"}
    if turn is not None:
        entry["turn"] = turn
    with _lock:
        _entries.append(entry)
    return entry["id"]


def recent(n: int = 40) -> list[dict]:
    with _lock:
        return [dict(e) for e in list(_entries)[-n:]]


def clear() -> None:
    with _lock:
        _entries.clear()
