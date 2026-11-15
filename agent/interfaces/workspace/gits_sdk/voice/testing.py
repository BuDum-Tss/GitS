"""Заглушка голосового адаптера для тестов: `FakeAdapter` ведёт себя как настоящий адаптер, но вместо звуковой карты или Discord
записывает то, что прислал движок, а речь «участников» синтезирует тоном. Тестовые генераторы звука: `tone`, `silence`."""
from __future__ import annotations

import asyncio
import math
import struct

from . import protocol as V
from .client import PlayStream, VoiceAdapterClient


def tone(ms: int, *, freq: float = 220.0, amplitude: float = 0.3) -> bytes:
    """Громкий тон заданной длины: для движка это «речь»."""
    n = V.SAMPLE_RATE * ms // 1000
    peak = int(32767 * amplitude)
    return struct.pack(f"<{n}h", *(int(peak * math.sin(2 * math.pi * freq * i / V.SAMPLE_RATE)) for i in range(n)))


def silence(ms: int) -> bytes:
    """Тишина заданной длины."""
    return bytes(V.SAMPLE_RATE * ms // 1000 * V.SAMPLE_WIDTH)


class FakeAdapter:
    """Адаптер без звука: собирает кадры реплик движка и умеет «говорить» от имени участников.

        fake = FakeAdapter(url, token, session="t1", echo_free=True)
        await fake.start()
        await fake.join("u1", "Маша")
        await fake.speak("u1", 1200)                 # 1.2 с «речи» и 0.8 с тишины после неё
        await fake.wait_played(count=1)
        fake.played[uid]                             # PCM, который прислал движок
    """

    def __init__(self, url: str, token: str, *, session: str = "fake-session", adapter: str = "fake", echo_free: bool = True,
                 can_barge_in: bool = True, speakers_labelled: bool = True, direct: bool = False, play_delay_s: float = 0.0,
                 **client_options):
        self.played: dict[str, bytearray] = {}
        self.texts: dict[str, str] = {}
        self.stops: list[tuple[str, str]] = []
        self.leaves = 0
        self.play_delay_s = play_delay_s
        self._done = asyncio.Event()
        self._count = 0
        self.client = VoiceAdapterClient(adapter=adapter, session=session, url=url, token=token, echo_free=echo_free,
                                         can_barge_in=can_barge_in, speakers_labelled=speakers_labelled, direct=direct,
                                         on_play=self._on_play, on_stop=self._on_stop, on_leave=self._on_leave,
                                         **client_options)

    async def start(self, timeout: float = 5.0) -> bool:
        await self.client.start()
        return await self.client.wait_ready(timeout)

    async def stop(self) -> None:
        await self.client.stop()

    async def join(self, speaker_id: str, name: str = "", *, is_owner: bool = False) -> int:
        return await self.client.speaker_join(speaker_id, name or speaker_id, is_owner=is_owner)

    async def leave(self, speaker_id: str) -> None:
        await self.client.speaker_leave(speaker_id)

    async def speak(self, speaker_id: str, speech_ms: int, *, tail_ms: int = 800, chunk_ms: int = 100, pace_s: float = 0.0) -> None:
        """Участник говорит `speech_ms` мс, затем молчит `tail_ms` мс (реплика закрывается хвостом тишины)."""
        audio = tone(speech_ms) + silence(tail_ms)
        step = V.SAMPLE_RATE * chunk_ms // 1000 * V.SAMPLE_WIDTH
        for off in range(0, len(audio), step):
            await self.client.send_audio(speaker_id, audio[off:off + step])
            if pace_s:
                await asyncio.sleep(pace_s)

    async def wait_played(self, count: int = 1, timeout: float = 5.0) -> None:
        """Дождаться, пока до конца принято `count` реплик (после возврата on_play)."""
        async def waiter():
            while self._count < count:
                self._done.clear()
                await self._done.wait()
        await asyncio.wait_for(waiter(), timeout)

    # ── обработчики клиента ─────────────────────────────────────────────────────

    async def _on_play(self, frames: PlayStream, uid: str) -> int:
        buf = self.played.setdefault(uid, bytearray())
        self.texts[uid] = frames.text
        async for pcm in frames:
            buf.extend(pcm)
            if self.play_delay_s:
                await asyncio.sleep(self.play_delay_s)
        self._count += 1
        self._done.set()
        return len(buf) * 1000 // (V.SAMPLE_RATE * V.SAMPLE_WIDTH)

    def _on_stop(self, uid: str, reason: str) -> None:
        self.stops.append((uid, reason))

    def _on_leave(self) -> None:
        self.leaves += 1
