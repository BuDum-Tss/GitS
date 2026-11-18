"""Помощники тестов внимания v2: поддельные часы (время идёт, только когда его двигают), заглушка Runtime, готовый реестр видов."""
import asyncio
import heapq
import itertools

from .event import Event
from .kinds import KindRegistry
from .runtime import ActionRecord


class FakeClock:
    """Время движется только `advance`; задачи успевают отработать между шагами. Реальных ожиданий нет."""

    def __init__(self, start: float = 1000.0):
        self._now = start
        self._epoch = 1_700_000_000.0 - start
        self._waiters: list[tuple[float, int, asyncio.Future]] = []
        self._ids = itertools.count()

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now + self._epoch

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (self._now + seconds, next(self._ids), fut))
        await fut

    async def settle(self, rounds: int = 30) -> None:
        for _ in range(rounds):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        """Подвинуть время на `seconds`, будя спящих по порядку сроков."""
        target = self._now + seconds
        await self.settle()
        while self._waiters and self._waiters[0][0] <= target:
            when, _, fut = heapq.heappop(self._waiters)
            self._now = max(self._now, when)
            if not fut.done():
                fut.set_result(None)
            await self.settle()
        self._now = target
        await self.settle()


class FakeRuntime:
    """Runtime-заглушка: записывает `trigger`, `interrupt`, `steer`, `trigger_idle` и имитирует занятость (`running`).

    `attention` (если задано) получает возврат прерванных событий и конец хода, как от настоящего Runtime:
    `had_effects` и `actions` задают, что успел сделать прерываемый ход."""

    def __init__(self, attention=None, clock=None):
        self.attention = attention
        self.clock = clock
        self.can_steer = True
        self.steer_result = True
        self.auto_busy = True                      # после trigger/interrupt агент занят новой пачкой
        self.running: list[Event] = []
        self.turn_started_at: float | None = None
        self.had_effects = False
        self.actions: list[ActionRecord] = []
        self.triggers: list[tuple[list[Event], int]] = []
        self.interrupts: list[tuple[list[Event], list[Event]]] = []        # (новые события, прерванная пачка)
        self.steers: list[Event] = []
        self.idle_triggers = 0
        self.notes: list[str] = []

    def current_batch(self) -> list[Event]:
        return list(self.running)

    def _start(self, events: list[Event]) -> None:
        if self.auto_busy:
            self.running = list(events)
        self.turn_started_at = self.clock.time() if self.clock is not None else None

    async def trigger(self, events: list[Event], after_ms: int = 0) -> None:
        if self.running:
            raise RuntimeError("trigger при идущем ходе")
        self.triggers.append((list(events), after_ms))
        self._start(events)

    async def interrupt(self, new_events: list[Event]) -> None:
        old = list(self.running)
        self.interrupts.append((list(new_events), old))
        if self.attention is not None:
            self.attention.requeue_interrupted(old, self.had_effects)
            self.notes.append(self.attention.build_interrupt_note(old, self.actions, self.turn_started_at) if self.had_effects else "")
        self.had_effects, self.actions = False, []
        self._start(new_events)

    async def steer(self, event: Event) -> bool:
        self.steers.append(event)
        if self.steer_result:
            self.running.append(event)
        return self.steer_result

    async def trigger_idle(self) -> None:
        self.idle_triggers += 1

    def set_batch(self, events: list[Event]) -> None:
        """Сделать агента занятым заданной пачкой (без `trigger`)."""
        self.running = list(events)

    async def finish_turn(self) -> None:
        """Ход закончился: пачка очищена, внимание получает хук `on_turn_end`."""
        self.running = []
        if self.attention is not None:
            await self.attention.on_turn_end()

    @property
    def last_trigger(self) -> list[Event]:
        return self.triggers[-1][0]


def default_registry() -> KindRegistry:
    """Виды для тестов: чат, голос, игра, таймер, итог речи; `critical` у тревоги игры."""
    reg = KindRegistry()
    reg.register_manifest("discord", {"event_kinds": [
        {"kind": "discord.message", "default_priority": "NORMAL", "max_priority": "IMPORTANT", "ttl_s": 900, "coalesce": True,
         "reply_tool": "discord.text"},
        {"kind": "discord.voice.utterance", "default_priority": "USER", "max_priority": "USER", "ttl_s": 90, "coalesce": True,
         "reply_tool": "discord.say"},
        {"kind": "discord.spoken", "default_priority": "BACKGROUND", "max_priority": "BACKGROUND", "ttl_s": 120},
        {"kind": "discord.delivery_failed", "default_priority": "NOTABLE", "max_priority": "NOTABLE", "ttl_s": 600},
    ]})
    reg.register_manifest("rimworld", {"event_kinds": [
        {"kind": "rimworld.event", "default_priority": "NORMAL", "max_priority": "IMPORTANT", "ttl_s": 60},
        {"kind": "rimworld.alert", "default_priority": "IMPORTANT", "max_priority": "IMPORTANT", "ttl_s": 60, "critical": True},
    ]})
    reg.register_manifest("test", {"event_kinds": [
        {"kind": "test.ev", "default_priority": "NORMAL", "max_priority": "USER", "ttl_s": 900, "coalesce": True},
        {"kind": "test.plain", "default_priority": "NORMAL", "max_priority": "USER", "ttl_s": 900},
    ]})
    reg.register_manifest("core", {"event_kinds": [
        {"kind": "core.timer", "default_priority": "NOTABLE", "max_priority": "IMPORTANT", "ttl_s": 600},
    ]})
    return reg


def make_event(clock, priority=None, *, workspace: str = "discord", kind: str = "discord.message", text: str = "текст",
               **kw) -> Event:
    """Событие с `ts` по поддельным часам."""
    kw.setdefault("ts", clock.time())
    return Event(workspace=workspace, kind=kind, text=text, priority=priority, **kw)


class Rig:
    """Внимание на заглушках: поддельные часы, реестр видов, журнал и FakeRuntime в одном месте (фокус по умолчанию `test`)."""

    def __init__(self, focus="test", **kw):
        from .attention import AttentionV2
        from .eventlog import EventLog
        self.clock = FakeClock()
        self.registry = default_registry()
        self.log = EventLog(clock=self.clock)
        self.runtime = FakeRuntime(clock=self.clock)
        self.focus = focus
        self.attention = AttentionV2(self.registry, self.log, self.runtime, self.clock, lambda: self.focus, **kw)
        self.runtime.attention = self.attention

    def ev(self, priority=None, **kw) -> Event:
        kw.setdefault("workspace", "test")
        kw.setdefault("kind", "test.ev")
        return make_event(self.clock, priority, **kw)

    async def send(self, priority=None, **kw) -> Event:
        e = self.ev(priority, **kw)
        await self.attention.process_event(e)
        return e
