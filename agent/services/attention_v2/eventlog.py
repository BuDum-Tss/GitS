"""Журнал событий внимания (RUNTIME_SPEC раздел 8): только дописывается; запись приёма и отдельная запись на каждый переход.

Хранилище кольцо в памяти; `sink` получает каждую запись (позже подключается к `services/tracing/events_log`).
Сырые аудио и приватные поля `data` в журнал не идут: пишутся только текст и метаданные."""
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable

from .event import Event, EventState

DEFAULT_MAX = 5000


@dataclass
class LogEntry:
    seq: int
    ts: float
    event_id: str
    op: str                                # new | state | note
    state: str
    workspace: str
    kind: str
    priority: float | None
    text: str = ""                         # только у записи `new`
    reason: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class EventLog:
    def __init__(self, *, max_entries: int = DEFAULT_MAX, sink: Callable[[dict], None] | None = None, clock=None):
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._sink = sink
        self._clock = clock
        self._seq = 0

    def _now(self) -> float:
        return self._clock.time() if self._clock is not None else time.time()

    def _add(self, e: Event, op: str, *, text: str = "", reason: str = "", extra: dict | None = None) -> LogEntry:
        self._seq += 1
        entry = LogEntry(seq=self._seq, ts=self._now(), event_id=e.id, op=op, state=e.state.value, workspace=e.workspace,
                         kind=e.kind, priority=e.priority, text=text, reason=reason, extra=dict(extra or {}))
        self._entries.append(entry)
        if self._sink is not None:
            try:
                self._sink(entry.to_dict())
            except Exception:                      # сбой приёмника не ломает внимание
                pass
        return entry

    def record_new(self, e: Event) -> LogEntry:
        """Приём события (состояние `new`)."""
        e.state = EventState.NEW
        return self._add(e, "new", text=e.text)

    def transition(self, e: Event, state: EventState, reason: str = "", **extra) -> LogEntry:
        """Переход состояния: меняет `e.state` и пишет отдельную запись со ссылкой на id."""
        e.state = state
        return self._add(e, "state", reason=reason, extra=extra)

    def note(self, e: Event, reason: str, **extra) -> LogEntry:
        """Пометка без смены состояния (зажим приоритета потолком, слияние)."""
        return self._add(e, "note", reason=reason, extra=extra)

    def __len__(self) -> int:
        return len(self._entries)

    def for_event(self, event_id: str) -> list[LogEntry]:
        return [x for x in self._entries if x.event_id == event_id]

    def states_of(self, event_id: str) -> list[str]:
        """Последовательность состояний события по журналу: `new`, затем каждый переход."""
        return [x.state for x in self._entries if x.event_id == event_id and x.op in ("new", "state")]

    def query(self, workspace: str | None = None, kind: str | None = None, since: float | None = None,
              limit: int | None = None) -> list[LogEntry]:
        """Записи по окружению, виду и времени (`ts` записи не раньше `since`); при `limit` самые свежие, порядок по времени."""
        out = [x for x in self._entries
               if (workspace is None or x.workspace == workspace) and (kind is None or x.kind == kind)
               and (since is None or x.ts >= since)]
        if limit is not None and limit >= 0:
            out = out[-limit:] if limit else []
        return out
