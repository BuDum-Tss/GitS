"""Приоритетная очередь внимания и сводка пропущенного (RUNTIME_SPEC 4.2 и 4.6)."""
import itertools
import time
from typing import Callable

from .event import Event, EventState
from .render import fmt_hm

QUEUE_MAX = 30


class Digest:
    """Сводка «пока тебя не было»: просроченные и вытесненные события считаются по окружению и виду, полный список в EventLog."""

    def __init__(self):
        self.counts: dict[tuple[str, str], int] = {}
        self.last_ts: float | None = None

    def add(self, e: Event) -> None:
        key = (e.workspace, e.kind)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.last_ts = e.ts if self.last_ts is None else max(self.last_ts, e.ts)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def __bool__(self) -> bool:
        return bool(self.counts)

    def render(self) -> str:
        """`(пропущено: 12 discord/message, 2 rimworld/alert; последнее 14:05)`; пустая строка, если нечего сообщать."""
        if not self.counts:
            return ""
        parts = [f"{n} {ws}/{kind[len(ws) + 1:] if kind.startswith(ws + '.') else kind}"
                 for (ws, kind), n in sorted(self.counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        tail = f"; последнее {fmt_hm(self.last_ts)}" if self.last_ts is not None else ""
        return "(пропущено: " + ", ".join(parts) + tail + ")"

    def take(self) -> str:
        """Строка сводки для начала пачки; счётчики обнуляются."""
        line = self.render()
        self.clear()
        return line

    def clear(self) -> None:
        self.counts.clear()
        self.last_ts = None


class PriorityQueue:
    """Порядок: приоритет по убыванию, при равенстве старые (по `ts`) первыми. Переполнение вытесняет самое низкое и самое старое
    (`evicted`), просроченные выбрасываются при каждом обращении (`expired`); обе категории копятся в `digest`."""

    def __init__(self, max_size: int = QUEUE_MAX, digest: Digest | None = None, clock=None,
                 on_drop: Callable[[Event, EventState], None] | None = None):
        self.max_size = max_size
        self.digest = digest if digest is not None else Digest()
        self._clock = clock
        self._on_drop = on_drop
        self._items: list[Event] = []
        self._seq: dict[str, int] = {}
        self._queued_at: dict[str, float] = {}
        self._counter = itertools.count()

    def _now(self) -> float:
        return self._clock.time() if self._clock is not None else time.time()

    def _drop(self, e: Event, state: EventState) -> None:
        self._remove(e)
        self.digest.add(e)
        if self._on_drop is not None:
            self._on_drop(e, state)

    def _remove(self, e: Event) -> None:
        self._items = [x for x in self._items if x.id != e.id]
        self._seq.pop(e.id, None)
        self._queued_at.pop(e.id, None)

    def _sorted(self) -> list[Event]:
        return sorted(self._items, key=lambda e: (-e.prio, e.ts, self._seq[e.id]))

    def expire(self) -> list[Event]:
        """Выбросить просроченные (`expired`); возвращает их."""
        now = self._now()
        gone = [e for e in self._items if e.is_expired(now)]
        for e in gone:
            self._drop(e, EventState.EXPIRED)
        return gone

    def push(self, e: Event) -> list[Event]:
        """Поставить событие; возвращает вытесненные при переполнении (возможно, само `e`, если оно самое слабое)."""
        self.expire()
        if e.id not in self._seq:
            self._items.append(e)
            self._seq[e.id] = next(self._counter)
            self._queued_at[e.id] = self._now()
        evicted = []
        while len(self._items) > self.max_size:
            victim = min(self._items, key=lambda x: (x.prio, x.ts, self._seq[x.id]))
            self._drop(victim, EventState.EVICTED)
            evicted.append(victim)
        return evicted

    def pop(self, n: int) -> list[Event]:
        """До `n` лучших событий (убирает из очереди)."""
        self.expire()
        out = self._sorted()[:max(0, n)]
        for e in out:
            self._remove(e)
        return out

    def max_priority(self) -> float:
        """Приоритет первого элемента, 0 для пустой очереди."""
        self.expire()
        return max((e.prio for e in self._items), default=0)

    def oldest_wait(self) -> float:
        """Сколько секунд ждёт самое давнее событие очереди (0 для пустой)."""
        if not self._queued_at:
            return 0.0
        return self._now() - min(self._queued_at.values())

    def oldest_queued_at(self) -> float | None:
        return min(self._queued_at.values()) if self._queued_at else None

    def find_group(self, workspace: str, kind: str, group_key: str) -> Event | None:
        """Ожидающее событие с этим `group_key` (самое свежее), с которым можно слить."""
        found = [e for e in self._items if e.group_key == group_key and e.kind == kind and e.workspace == workspace]
        return max(found, key=lambda e: self._seq[e.id], default=None)

    def items(self) -> list[Event]:
        return self._sorted()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, e: Event) -> bool:
        return e.id in self._seq
