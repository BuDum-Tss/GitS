"""Голосовой адаптер «виртуальные кабели» (adapter="cable"): звонок идёт через стороннюю программу (Discord-клиент, Telegram, Zoom).

Звук собеседников программа выводит на кабель A, мы пишем с его выхода; её голос мы играем на кабель B, который в программе
выбран микрофоном. Один «говорящий» с id `call`: звук всех собеседников приходит смешанным, поэтому `speakers_labelled=False`.
Её голос на вход не попадает (другой кабель), значит `echo_free=True` и перебивание разрешено. Вход и выход одного кабеля
(петля) адаптер не принимает: она услышала бы сама себя.

Звук не пишется на диск и не попадает в журнал, только счётчики.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from cable_io import AudioIn, AudioOut, LoopError, same_cable
from gits_sdk.voice import (PlayStream, VoiceAdapterClient, to_16k_mono_stream,
                            to_48k_stereo_stream)

logger = logging.getLogger("gits.audio_cable")

ADAPTER = "cable"
SPEAKER_ID = "call"
BACKPRESSURE_MS = 200                 # столько звука держим в буфере выхода; больше не подаём, ждём
SEND_QUEUE_FRAMES = 25                # кадров (по 20 мс) до отправки в движок; при переполнении старые отбрасываются
READY_POLL_S = 1.0


class CableAdapter:
    def __init__(self, audio_in: AudioIn, audio_out: AudioOut, *, session: str, name: str = "Собеседник",
                 is_owner: bool = False, title: str = "", url: str | None = None, token: str | None = None) -> None:
        in_name, out_name = audio_in.device_name, audio_out.device_name
        if same_cable(in_name, out_name):
            raise LoopError(f"вход «{in_name}» и выход «{out_name}» это один кабель: она услышит себя. "
                            "Нужны два разных кабеля (A на вход, B на выход)")
        self.audio_in, self.audio_out = audio_in, audio_out
        self.name, self.is_owner = name, is_owner
        self.client = VoiceAdapterClient(adapter=ADAPTER, session=session, url=url, token=token, title=title,
                                         echo_free=True, can_barge_in=True, speakers_labelled=False, direct=False,
                                         on_play=self._on_play, on_stop=self._on_stop, on_leave=self._on_leave)
        self._tasks: list[asyncio.Task] = []
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._playing: str | None = None
        self._joined = False
        self.frames_received = 0
        self.frames_sent = 0
        self.frames_dropped = 0
        self.plays = 0
        self.aborts = 0
        self.leaves = 0

    # ── жизненный цикл ──

    async def start(self) -> None:
        await self.audio_out.start()
        try:
            await self.audio_in.start()
        except Exception:
            await self.audio_out.stop()
            raise
        await self.client.start()
        loop = asyncio.get_running_loop()
        self._tasks = [loop.create_task(self._rx(), name="cable-rx"), loop.create_task(self._tx(), name="cable-tx")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        await self.client.stop()
        await self.audio_in.stop()
        await self.audio_out.stop()

    async def wait_ready(self, timeout: float = 10.0) -> bool:
        return await self.client.wait_ready(timeout)

    # ── приём: кабель A -> движок ──

    async def _rx(self) -> None:
        """Кадры входа -> 16 кГц моно -> очередь отправки. Пока клиент не готов, звук отбрасывается и считается."""
        down = to_16k_mono_stream()
        async for frame in self.audio_in:
            self.frames_received += 1
            if not self.client.ready:
                self.frames_dropped += 1
                continue
            if not self._joined:
                await self.client.speaker_join(SPEAKER_ID, self.name, is_owner=self.is_owner)
                self._joined = True                       # после переподключения клиент объявит говорящего сам
            pcm = down.process(frame)
            if not pcm:
                continue
            if self._send_queue.qsize() >= SEND_QUEUE_FRAMES:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._send_queue.get_nowait()         # не блокируемся: живую речь не догоняют
                self.frames_dropped += 1
            self._send_queue.put_nowait(pcm)

    async def _tx(self) -> None:
        while True:
            pcm = await self._send_queue.get()
            if not self.client.ready:
                self.frames_dropped += 1
                continue
            if await self.client.send_audio(SPEAKER_ID, pcm):
                self.frames_sent += 1
            else:
                self.frames_dropped += 1

    # ── воспроизведение: движок -> кабель B ──

    async def _on_play(self, frames: PlayStream, uid: str) -> int:
        out = self.audio_out
        if self._playing is not None:                     # старая реплика ещё идёт: новая её заменяет
            out.abort()
        self._playing = uid
        out.begin()
        up = to_48k_stereo_stream()
        self.plays += 1
        try:
            async for pcm in frames:
                if frames.stop_reason is not None:
                    break
                out.feed(up.process(pcm))
                while out.buffered_ms > BACKPRESSURE_MS and frames.stop_reason is None:
                    await asyncio.sleep(0.02)
            if frames.stop_reason is None:
                out.feed(up.flush())
                await out.drained()
            return out.played_ms
        except asyncio.CancelledError:
            out.abort()
            raise
        finally:
            if self._playing == uid:
                self._playing = None

    def _on_stop(self, uid: str, reason: str) -> None:
        """Остановка реплики (перебивание, команда): буфер выхода сбрасывается сразу и синхронно."""
        self.audio_out.abort()
        self.aborts += 1
        logger.info("кабель: реплика остановлена (%s)", reason)

    def _on_leave(self) -> None:
        """Движок просит выйти из канала; из чужой программы звонка выйти нечем, адаптер остаётся."""
        self.leaves += 1
        logger.info("кабель: движок просит выйти из канала (нечем: звонок идёт в стороннем клиенте)")

    # ── состояние ──

    def status(self) -> dict:
        return {
            "adapter": ADAPTER, "session": self.client.session_id, "ready": self.client.ready,
            "connects": self.client.stats.connects, "input": self.audio_in.device_name, "output": self.audio_out.device_name,
            "frames_received": self.frames_received, "frames_sent": self.frames_sent, "frames_dropped": self.frames_dropped,
            "input_dropped": self.audio_in.dropped, "plays": self.plays, "aborts": self.aborts, "leaves": self.leaves,
            "last_error": self.client.last_error,
        }
