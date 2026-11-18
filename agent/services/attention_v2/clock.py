"""Часы и планировщик внимания v2: реальные в работе, поддельные в тестах (`testing.FakeClock`)."""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Protocol

log = logging.getLogger("attention_v2")


class SystemClock:
    """Реальное время: `time()` для меток событий и сроков, `monotonic()` для интервалов."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class TimerHandle(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    """Разовые отложенные вызовы (окно слияния, таймер `ON_TURN_S`). Периодического тика нет."""

    def call_later(self, delay_s: float, fn: Callable[[], Awaitable[None]]) -> TimerHandle: ...


class ClockScheduler:
    """Планировщик поверх `clock.sleep`: с поддельными часами таймеры срабатывают от `advance`, без реального ожидания."""

    def __init__(self, clock):
        self.clock = clock
        self._tasks: set[asyncio.Task] = set()

    def call_later(self, delay_s: float, fn: Callable[[], Awaitable[None]]) -> TimerHandle:
        async def run():
            await self.clock.sleep(delay_s)
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("отложенный вызов внимания упал")

        task = asyncio.get_running_loop().create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
