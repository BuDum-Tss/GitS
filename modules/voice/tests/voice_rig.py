"""Стенд тестов модуля голоса: настоящий ModuleClient против FakeCore, синтез, воспроизведение и STT подменены."""
import asyncio
import contextlib
from pathlib import Path

import numpy as np
from gits_sdk import ModuleClient
from gits_sdk.testing import FakeCore

from app import VoiceModule
from manifest import build_manifest
from stt_client import Transcript

SHARED = {"voice_muted": False, "volume": 100, "mic": False, "hearing": False, "dnd": False, "quiet_until": 0.0}


async def until(predicate, timeout: float = 5.0):
    """Ждать, пока predicate() не станет истинным (опрос раз в 5 мс)."""
    async def waiter():
        while True:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.005)
    return await asyncio.wait_for(waiter(), timeout)


class FakeSynth:
    """Синтез в памяти: что просили, что «не получилось», сколько «думает»."""

    def __init__(self, delay: float = 0.0):
        self.calls: list[str] = []
        self.fail: set[str] = set()
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, text: str) -> str | None:
        self.calls.append(text)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return None if text in self.fail else f"/fake/{len(self.calls)}.wav"


class FakePlayer:
    """Воспроизведение в памяти: играет `seconds` или, пока `hold`, ждёт `finish()`."""

    def __init__(self, seconds: float = 0.0, hold: bool = False):
        self.seconds, self.hold = seconds, hold
        self.played: list[str] = []
        self.volumes: list[float] = []
        self.cancelled = 0
        self.fail_next = False
        self._release = asyncio.Event()

    def finish(self) -> None:
        self._release.set()

    async def play(self, path: str, volume: float) -> None:
        self.played.append(path)
        self.volumes.append(volume)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("колонок нет")
        try:
            if self.hold:
                await self._release.wait()
                self._release.clear()
            else:
                await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FakeSTT:
    """WhisperX в памяти: отдаёт заданный текст и эмбеддинг."""

    mode = "server"
    diarized = True

    def __init__(self, text="", emb=None, speaker="SPEAKER_00"):
        self.t = Transcript(text=text, speaker=speaker, speaker_embedding=emb)
        self.calls = 0

    def label(self) -> str:
        return "распознавание: WhisperX (сервер), с узнаванием голосов"

    def short(self) -> str:
        return "WhisperX"

    async def transcribe(self, *a, **k):
        self.calls += 1
        return self.t

    async def aclose(self):
        pass


def vec(seed: int, n: int = 64) -> list[float]:
    return list(np.random.default_rng(seed).normal(size=n))


class Rig:
    """FakeCore + настоящий клиент + VoiceModule с подменёнными синтезом, воспроизведением и STT."""

    def __init__(self, tmp_path, *, settings=None, module_settings=None, player=None, stt=None, synth=None, hold=False):
        self.tmp = Path(tmp_path)
        self.mem = self.tmp / "memory"
        self.mem.mkdir(parents=True, exist_ok=True)
        self.core = FakeCore(settings={**SHARED, **(settings or {})}, module_settings=module_settings)
        self.synth_calc = synth or FakeSynth()
        self._player = player
        self._hold = hold
        self.stt = stt or FakeSTT()
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        await self.core.start()
        self.client = ModuleClient(build_manifest(), url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.player = self._player or FakePlayer(hold=self._hold)

        def synth(text, directory):                    # блокирующий синтез: путь к «файлу»
            raise AssertionError("в стенде синтез идёт через FakeSynth")

        self.mod = VoiceModule(self.client, str(self.mem), fake=False, player=self.player, stt=self.stt,
                               tmp_dir=self.tmp / "voice_tmp", synth=synth)
        self.mod.speaker._synth = self.synth_calc      # синтез сегмента для озвучки
        self._task = asyncio.create_task(self.client.run())
        await self.core.wait_connected()
        await self.client.wait_connected(5)
        self.mod.outbox.start()
        self.mod.speaker.start()
        return self

    async def __aexit__(self, *exc):
        await self.mod.speaker.aclose()
        await self.mod.outbox.stop()
        await self.client.stop()
        if self._task is not None and not self._task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
        await self.core.stop()

    # ── удобства ──

    def speech(self, phase: str | None = None) -> list[dict]:
        """voice.speech события, в порядке прихода: их data."""
        return [e["data"] for e in self.core.events if e["kind"] == "voice.speech"
                and (phase is None or e["data"]["phase"] == phase)]

    def kinds(self) -> list[str]:
        return [e["kind"] for e in self.core.events]

    async def speak(self, uid: str, text: str, **kw) -> None:
        await self.core.send("speak", {"utterance_id": uid, "text": text, **kw})

    async def stop(self, uid=None, at="segment_end") -> None:
        await self.core.send("speak.stop", {"utterance_id": uid, "at": at})

    async def wait_ended(self, uid: str, timeout: float = 5.0) -> dict:
        await until(lambda: any(d["utterance_id"] == uid for d in self.speech("ended")), timeout)
        return next(d for d in self.speech("ended") if d["utterance_id"] == uid)
