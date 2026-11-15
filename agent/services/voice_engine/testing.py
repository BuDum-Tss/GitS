"""Помощники тестов движка: поддельные часы (время идёт, только когда его двигают) и канал-заглушка вместо WebSocket."""
import asyncio
import heapq
import itertools

from .protocol import TO_ADAPTER, decode


class FakeClock:
    """Время движется только `advance`: кадры по 20 мс проверяются точно и без ожидания. Задачи успевают отработать между шагами."""

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


class RecordingTransport:
    """Канал к адаптеру в памяти: записывает всё, что движок отправил, и время каждой отправки."""

    def __init__(self, clock=None):
        self.clock = clock
        self.controls: list[dict] = []
        self.frames: list[tuple[float, int, int, int]] = []       # (время, play_id, seq, байт)
        self.closed = False
        self.close_reason: str | None = None
        self.fail = False

    def _now(self) -> float:
        return self.clock.monotonic() if self.clock is not None else 0.0

    async def send_control(self, text: str) -> None:
        if self.fail or self.closed:
            raise ConnectionError("канал закрыт")
        self.controls.append(decode(text, allowed=TO_ADAPTER))

    async def send_play_frame(self, play_id: int, seq: int, pcm: bytes) -> None:
        if self.fail or self.closed:
            raise ConnectionError("канал закрыт")
        self.frames.append((self._now(), play_id, seq, len(pcm)))

    async def close(self, reason: str) -> None:
        self.closed, self.close_reason = True, reason

    def types(self) -> list[str]:
        return [c["type"] for c in self.controls]

    def of(self, kind: str) -> list[dict]:
        return [c for c in self.controls if c["type"] == kind]





class ClockTts:
    """Синтез на поддельных часах: время ответа задаётся (`latencies` по вызовам, затем `latency_s`), звук длиной `ms_per_char`
    на символ. Помнит тексты, одновременные вызовы (`max_active`) и может падать на заданных текстах."""

    def __init__(self, clock: FakeClock, *, ms_per_char: int = 40, latency_s: float = 0.5, latencies: list[float] | None = None,
                 fail_on: tuple[str, ...] = ()):
        self.clock, self.ms_per_char, self.latency_s = clock, ms_per_char, latency_s
        self.latencies = list(latencies or [])
        self.fail_on = fail_on
        self.calls: list[str] = []
        self.active = self.max_active = 0

    async def synthesize(self, text: str, *, voice=None, emotion=None) -> bytes:
        from .tts import TtsServerError
        self.calls.append(text)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await self.clock.sleep(self.latencies.pop(0) if self.latencies else self.latency_s)
            if any(f in text for f in self.fail_on):
                raise TtsServerError(500)
            return b"\x10\x00" * (len(text) * self.ms_per_char * 16)
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        pass
