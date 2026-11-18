"""CableAdapter против настоящего движка голоса в процессе (FakeStt и FakeTts, MemoryIn и MemoryOut): без звука и сети."""
import asyncio

import numpy as np
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from cable_adapter import ADAPTER, SPEAKER_ID, CableAdapter
from cable_io import FRAME_BYTES, LoopError, MemoryIn, MemoryOut
from gits_sdk.voice import protocol as V
from gits_sdk.voice import to_48k_stereo
from gits_sdk.voice.testing import silence, tone
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.server import VoiceServer
from services.voice_engine.stt import FakeStt
from services.voice_engine.tts import FakeTts

TOKEN = "тестовый-токен"
SESSION = "cable-test"


def frames48(ms: int, *, speech: bool) -> list[bytes]:
    """Кадры входа (48 кГц стерео, 20 мс): тон («речь») или тишина."""
    pcm = to_48k_stereo(tone(ms) if speech else silence(ms))
    return [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]


class Env:
    def __init__(self):
        self.events: list[dict] = []
        self.speech: list[dict] = []

        async def emit(event):
            self.events.append(event)

        self.engine = VoiceEngine(stt=FakeStt(default="Глэдос, слышишь меня"), tts=FakeTts(ms_per_char=10),
                                  config=EngineConfig(barge_in_ms=400), emit_event=emit, journal=lambda k, t, d: None,
                                  on_speech=self.speech.append, names=lambda: ("Глэдос",))
        self.voice = VoiceServer(self.engine, lambda: TOKEN, hello_timeout_s=0.5)
        app = web.Application()
        self.voice.attach(app)
        self.server = TestServer(app)
        self.adapters: list[CableAdapter] = []

    async def start(self):
        await self.server.start_server()
        self.url = f"ws://127.0.0.1:{self.server.port}{V.WS_PATH}"

    async def stop(self):
        for a in self.adapters:
            await a.stop()
        await self.engine.aclose()
        await self.server.close()

    async def adapter(self, *, frame_delay_s: float = 0.0, **kw) -> tuple[CableAdapter, MemoryIn, MemoryOut]:
        mic, spk = MemoryIn(keep_open=True, pace_s=0.002, device_name="CABLE-A Output"), MemoryOut(frame_delay_s=frame_delay_s, device_name="CABLE-B Input")
        a = CableAdapter(mic, spk, session=SESSION, url=self.url, token=TOKEN, **kw)
        self.adapters.append(a)
        await a.start()
        assert await a.wait_ready(5)
        mic.push(silence_frame())                                  # первый кадр после готовности объявляет говорящего
        await self.until(lambda: self.engine.session(SESSION) is not None and SPEAKER_ID in self.engine.session(SESSION).speakers)
        return a, mic, spk

    async def until(self, cond, timeout=5.0):
        async def poll():
            while not cond():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)


def silence_frame() -> bytes:
    return bytes(FRAME_BYTES)


def say(mic: MemoryIn, ms: int = 700, tail_ms: int = 800) -> None:
    for f in frames48(ms, speech=True) + frames48(tail_ms, speech=False):
        mic.push(f)


@pytest.fixture
async def env():
    e = Env()
    await e.start()
    yield e
    await e.stop()


# ── отказ при петле ──

@pytest.mark.parametrize("in_name,out_name", [
    ("CABLE Output (VB-Audio Virtual Cable)", "CABLE Input (VB-Audio Virtual Cable)"),
    ("CABLE-A Output", "CABLE-A Output"),
])
def test_loop_refused(in_name, out_name):
    with pytest.raises(LoopError) as err:
        CableAdapter(MemoryIn(device_name=in_name), MemoryOut(device_name=out_name), session="x")
    assert "один кабель" in str(err.value)


def test_different_cables_accepted():
    CableAdapter(MemoryIn(device_name="CABLE-A Output"), MemoryOut(device_name="CABLE-B Input"), session="x")


# ── сквозные сценарии ──

async def test_hello_capabilities_and_speaker(env):
    a, mic, spk = await env.adapter(name="Маша", is_owner=True, title="Звонок")
    assert a.client.capabilities == {"echo_free": True, "can_barge_in": True, "speakers_labelled": False, "direct": False}
    sess = env.engine.session(SESSION)
    assert sess.adapter == ADAPTER
    sp = sess.speakers[SPEAKER_ID]
    assert sp.name == "Маша" and sp.is_owner is True


async def test_call_speech_reaches_engine(env):
    a, mic, spk = await env.adapter(name="Маша")
    say(mic)
    await env.until(lambda: env.events)
    ev = env.events[0]
    assert ev["data"]["phrase"] == "Глэдос, слышишь меня" and ev["sender"]["id"] == SPEAKER_ID and ev["sender"]["name"] == "Маша"
    assert ev["data"]["adapter"] == ADAPTER
    st = a.status()
    assert st["frames_sent"] > 40 and st["frames_dropped"] == 0 and st["ready"] is True and st["input"] == "CABLE-A Output" and st["output"] == "CABLE-B Input"


async def test_reply_is_played_as_48k_stereo(env):
    a, mic, spk = await env.adapter()
    ticket = env.engine.speak(SESSION, "а" * 100)                  # 100 символов по 10 мс = 1 с
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    ended = [s for s in env.speech if s["phase"] == "ended"][0]
    assert ended["completed"] is True and ended["utterance_id"] == ticket.uid and ended["spoken_ms"] >= 1000
    assert len(spk.audio) % FRAME_BYTES == 0
    assert 50 <= len(spk.audio) // FRAME_BYTES <= 52               # 1 с = 50 кадров по 20 мс, плюс хвост фильтра
    samples = np.frombuffer(bytes(spk.audio), dtype="<i2").reshape(-1, 2)
    assert np.array_equal(samples[:, 0], samples[:, 1])            # стерео: каналы одинаковы
    assert np.abs(samples[1000:40000, 0]).mean() > 8               # звук пришёл (постоянный тон FakeTts, не тишина)
    assert a.status()["plays"] == 1 and a.status()["aborts"] == 0


async def test_stop_interrupts_within_frames(env):
    a, mic, spk = await env.adapter(frame_delay_s=0.02)           # выход играет «вживую»: 20 мс на кадр
    ticket = env.engine.speak(SESSION, "а" * 400)                  # 4 с
    await env.until(lambda: len(spk.audio) >= FRAME_BYTES * 5)
    env.engine.stop(SESSION, uid=ticket.uid, reason="stop")
    await env.until(lambda: a.aborts == 1)
    seen = len(spk.audio)
    await asyncio.sleep(0.15)
    assert len(spk.audio) - seen <= 2 * FRAME_BYTES               # дописывается не больше пары кадров
    assert spk.buffered_ms == 0
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    ended = [s for s in env.speech if s["phase"] == "ended"][0]
    assert ended["completed"] is False and 0 < ended["spoken_ms"] < 1500


async def test_speech_from_call_barges_in(env):
    a, mic, spk = await env.adapter(frame_delay_s=0.02)
    env.engine.speak(SESSION, "а" * 400)
    await env.until(lambda: len(spk.audio) >= FRAME_BYTES * 5)
    for f in frames48(700, speech=True):                           # собеседник перебивает
        mic.push(f)
    await env.until(lambda: a.aborts >= 1)
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    ended = [s for s in env.speech if s["phase"] == "ended"][0]
    assert ended["reason"] == "barge_in" and 0 < ended["spoken_ms"] < 4000


async def test_reconnect_restores_speaker(env, monkeypatch):
    monkeypatch.setattr("gits_sdk.voice.client.RECONNECT_DELAYS", (0.05,))
    a, mic, spk = await env.adapter(name="Маша")
    first = env.engine.session(SESSION)
    await env.engine.close_session(SESSION, "перезапуск")
    await env.until(lambda: env.engine.session(SESSION) not in (None, first) and SPEAKER_ID in env.engine.session(SESSION).speakers)
    assert a.client.stats.connects == 2 and env.engine.session(SESSION).speakers[SPEAKER_ID].name == "Маша"
    say(mic)                                                       # и звук снова доходит
    await env.until(lambda: env.events)
    assert env.events[0]["sender"]["id"] == SPEAKER_ID


async def test_audio_before_ready_is_dropped_not_sent():
    """Движка нет: кадры входа отбрасываются и считаются, адаптер не блокируется и не копит звук."""
    mic, spk = MemoryIn(frames48(200, speech=True)), MemoryOut()
    a = CableAdapter(mic, spk, session=SESSION, url="ws://127.0.0.1:1/voice/ws", token=TOKEN)
    await a.start()
    for _ in range(100):
        if a.frames_received >= 10:
            break
        await asyncio.sleep(0.01)
    st = a.status()
    assert st["ready"] is False and st["frames_received"] == 10 and st["frames_dropped"] == 10 and st["frames_sent"] == 0
    await a.stop()


async def test_leave_is_only_counted(env):
    a, mic, spk = await env.adapter()
    await a.client._on_text('{"type": "leave"}')
    assert a.leaves == 1 and a.client.ready
