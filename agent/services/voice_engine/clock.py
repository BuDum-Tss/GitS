"""Часы движка голоса: реальные в работе, поддельные в тестах (темп кадров 20 мс проверяется без ожидания)."""
import asyncio
import time


class SystemClock:
    """Реальное время: монотонные секунды для интервалов, `time()` для меток событий."""

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
