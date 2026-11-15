"""WebSocket `/voice/ws` с настоящим aiohttp и клиентом SDK: от звука участника до события и от `speak` до кадров адаптеру."""
import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gits_sdk.voice import protocol as V
from gits_sdk.voice.client import VoiceAdapterClient
from gits_sdk.voice.testing import FakeAdapter, silence, tone
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.server import VoiceServer
from services.voice_engine.stt import FakeStt
from services.voice_engine.tts import FakeTts

TOKEN = "тестовый-токен"


class Env:
    def __init__(self):
        self.events: list[dict] = []
        self.speech: list[dict] = []
        self.journal: list[tuple] = []
        self.stt = FakeStt(default="Глэдос, слышишь меня")

        async def emit(event):
            self.events.append(event)

        self.engine = VoiceEngine(stt=self.stt, tts=FakeTts(ms_per_char=10), config=EngineConfig(), emit_event=emit,
                                  journal=lambda k, t, d: self.journal.append((k, t, d)), on_speech=self.speech.append,
                                  names=lambda: ("Глэдос",))
        self.voice = VoiceServer(self.engine, lambda: TOKEN, hello_timeout_s=0.5)
        app = web.Application()
        self.voice.attach(app)
        self.server = TestServer(app)

    async def start(self):
        await self.server.start_server()
        self.url = f"ws://127.0.0.1:{self.server.port}{V.WS_PATH}"

    async def stop(self):
        await self.engine.aclose()
        await self.server.close()

    def adapter(self, **kw) -> FakeAdapter:
        return FakeAdapter(self.url, TOKEN, session=kw.pop("session", "s1"), **kw)

    async def until(self, cond, timeout=5.0):
        async def poll():
            while not cond():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), timeout)


@pytest.fixture
async def env():
    e = Env()
    await e.start()
    yield e
    await e.stop()


async def raw_ws(env: Env, token=TOKEN):
    http = aiohttp.ClientSession()
    ws = await http.ws_connect(env.url, headers={"Authorization": f"Bearer {token}"}, max_msg_size=0)
    return http, ws


def hello_text(**over):
    body = {"version": 1, "adapter": "raw", "session": "r1", "capabilities": {}, "format": V.audio_format()}
    body.update(over)
    return V.encode("hello", **body)


# ── сквозной сценарий ───────────────────────────────────────────────────────────

async def test_end_to_end_utterance_and_speech(env):
    fake = env.adapter()
    assert await fake.start()
    assert fake.client.welcome["settings"]["barge_in"] == "any_speaker"
    await fake.join("u1", "Маша", is_owner=True)
    await env.until(lambda: "u1" in (env.engine.session("s1").speakers if env.engine.session("s1") else {}))
    await fake.speak("u1", 700)
    await env.until(lambda: env.events)
    ev = env.events[0]
    assert ev["text"] == "Глэдос, слышишь меня" and ev["sender"] == {"id": "u1", "name": "Маша", "is_owner": True}
    assert ev["addressed_to_me"] is True and ev["data"]["adapter"] == "fake" and ev["data"]["session"] == "s1"

    ticket = env.engine.speak("s1", "привет, Маша")             # 12 символов по 10 мс = 120 мс
    await fake.wait_played(1)
    assert fake.texts[ticket.uid] == "привет, Маша"
    assert len(fake.played[ticket.uid]) == 640 * 6               # 120 мс = 6 кадров по 20 мс, без потерь и дублей
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    ended = [s for s in env.speech if s["phase"] == "ended"][0]
    assert ended["completed"] is True and ended["spoken_upto"] == 120 and ended["utterance_id"] == ticket.uid
    assert env.journal[-1][0] == "module_out" and env.journal[-1][1] == "привет, Маша"
    await fake.stop()


async def test_adapter_disconnect_closes_session(env):
    fake = env.adapter()
    await fake.start()
    await fake.join("u1", "Маша")
    await env.until(lambda: env.engine.session("s1") is not None)
    env.engine.speak("s1", "а" * 100)                            # длинная реплика уже в очереди
    await fake.stop()
    await env.until(lambda: env.engine.session("s1") is None)
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    assert [s for s in env.speech if s["phase"] == "ended"][0]["reason"] == "closed"


async def test_barge_in_over_the_wire(env):
    fake = env.adapter(play_delay_s=0.02)
    await fake.start()
    await fake.join("u1", "Маша")
    await env.until(lambda: env.engine.session("s1") is not None and "u1" in env.engine.session("s1").speakers)
    ticket = env.engine.speak("s1", "а" * 400)                    # 4 секунды
    await env.until(lambda: ticket.uid in fake.played and len(fake.played[ticket.uid]) > 0)
    await fake.speak("u1", 600, tail_ms=0)                        # перебивает
    await env.until(lambda: fake.stops)
    assert fake.stops[0] == (ticket.uid, "barge_in")
    await env.until(lambda: any(s["phase"] == "ended" for s in env.speech))
    ended = [s for s in env.speech if s["phase"] == "ended"][0]
    assert ended["reason"] == "barge_in" and 0 < ended["spoken_upto"] < 4000
    assert env.engine.session("s1").stats.barge_ins == 1
    await fake.stop()


async def test_pc_adapter_without_echo_free_never_barges_in(env):
    fake = env.adapter(echo_free=False, can_barge_in=True, speakers_labelled=False)
    await fake.start()
    assert fake.client.welcome["settings"]["barge_in"] == "off"
    await fake.join("owner", "Хозяин", is_owner=True)
    await env.until(lambda: env.engine.session("s1") is not None and "owner" in env.engine.session("s1").speakers)
    ticket = env.engine.speak("s1", "а" * 100)                    # 1 секунда
    await fake.speak("owner", 600, tail_ms=0)
    await fake.wait_played(1)
    assert fake.stops == [] and len(fake.played[ticket.uid]) == 640 * 50
    await fake.stop()


async def test_client_reconnects_and_reannounces_speakers(env, monkeypatch):
    monkeypatch.setattr("gits_sdk.voice.client.RECONNECT_DELAYS", (0.05,))
    fake = env.adapter()
    await fake.start()
    await fake.join("u1", "Маша")
    await env.until(lambda: env.engine.session("s1") is not None and "u1" in env.engine.session("s1").speakers)
    first = env.engine.session("s1")
    await env.engine.close_session("s1", "сервер перезапускает")          # движок закрыл соединение
    await env.until(lambda: env.engine.session("s1") not in (None, first) and "u1" in env.engine.session("s1").speakers)
    assert fake.client.stats.connects == 2
    await fake.speak("u1", 600)                                          # и звук снова доходит
    await env.until(lambda: env.events)
    await fake.stop()


async def test_two_adapters_two_sessions(env):
    a, b = env.adapter(session="d1", adapter="discord"), env.adapter(session="pc", adapter="pc", echo_free=False)
    assert await a.start() and await b.start()
    await a.join("u1", "Маша")
    await b.join("owner", "Хозяин")
    await env.until(lambda: len(env.engine.sessions()) == 2 and all(s["speakers"] for s in env.engine.sessions()))
    env.engine.speak("pc", "привет")
    await b.wait_played(1)
    assert not a.played
    await a.stop()
    await b.stop()


# ── защита ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("header", [None, "Bearer чужой", "Basic abc"])
async def test_wrong_token_rejected(env, header):
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await http.ws_connect(env.url, headers={"Authorization": header} if header else {})
    assert err.value.status == 401 and env.voice.rejected == 1 and env.engine.sessions() == []


async def test_client_with_wrong_token_never_ready(env):
    c = VoiceAdapterClient(adapter="x", session="s9", url=env.url, token="плохой", reconnect=False)
    await c.start()
    assert not await c.wait_ready(0.5)
    await c.stop()
    assert env.engine.sessions() == []


async def test_forbidden_address_rejected():
    e = Env()
    e.voice._allow = lambda remote: False
    await e.start()
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await http.ws_connect(e.url, headers={"Authorization": f"Bearer {TOKEN}"})
    assert err.value.status == 403
    await e.stop()


async def test_first_message_must_be_valid_hello(env):
    http, ws = await raw_ws(env)
    await ws.send_str("не json")
    msg = await ws.receive(timeout=2)
    assert json.loads(msg.data)["code"] == "protocol"
    assert (await ws.receive(timeout=2)).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
    await http.close()
    http, ws = await raw_ws(env)
    await ws.send_str(hello_text(session="r2"))
    assert json.loads((await ws.receive(timeout=2)).data)["type"] == "welcome"
    await http.close()


async def test_hello_timeout(env):
    http, ws = await raw_ws(env)
    msg = await ws.receive(timeout=3)                             # hello не шлём: ждём отказ по таймауту
    assert json.loads(msg.data)["code"] == "timeout"
    await http.close()


async def test_session_limit_rejected():
    e = Env()
    e.engine.sessions_manager.max_sessions = 1
    await e.start()
    a = e.adapter(session="one")
    assert await a.start()
    http, ws = await raw_ws(e)
    await ws.send_str(hello_text(session="two"))
    assert json.loads((await ws.receive(timeout=2)).data)["code"] == "limit"
    await http.close()
    await a.stop()
    await e.stop()


async def test_garbage_after_hello_gets_errors_then_close(env):
    http, ws = await raw_ws(env)
    await ws.send_str(hello_text())
    assert json.loads((await ws.receive(timeout=2)).data)["type"] == "welcome"
    await ws.send_bytes(b"xx")                                     # короче заголовка
    await ws.send_bytes(V.HEADER.pack(V.KIND_PLAY, 0, 0, 0, 0) + bytes(640))     # чужой тип кадра
    await ws.send_bytes(V.HEADER.pack(V.KIND_MIC, 0, 1, 0, 0) + bytes(V.MAX_PCM_BYTES + 2))    # слишком большой
    await ws.send_str('{"type": "speaker.join"}')                  # без полей
    await ws.send_str('{"type": "welcome"}')                       # не то направление
    codes = [json.loads((await ws.receive(timeout=2)).data)["code"] for _ in range(5)]
    assert codes == ["protocol"] * 5
    assert env.engine.session("r1") is not None
    for _ in range(25):                                            # упорный мусор: соединение закрывается
        await ws.send_bytes(b"x")
    closed = False
    for _ in range(40):
        msg = await ws.receive(timeout=2)
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            closed = True
            break
    assert closed
    await env.until(lambda: env.engine.session("r1") is None)
    await http.close()


async def test_oversize_message_closes_connection(env):
    http, ws = await raw_ws(env)
    await ws.send_str(hello_text())
    await ws.receive(timeout=2)
    await ws.send_bytes(bytes(V.MAX_WS_MESSAGE_BYTES + 100))
    for _ in range(5):
        msg = await ws.receive(timeout=2)
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
    assert ws.closed or msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING)
    await env.until(lambda: env.engine.session("r1") is None)
    await http.close()


async def test_audio_of_unannounced_speaker_is_counted_not_fatal(env):
    http, ws = await raw_ws(env)
    await ws.send_str(hello_text())
    await ws.receive(timeout=2)
    await ws.send_bytes(V.pack_frame(V.KIND_MIC, 5, 0, 0, silence(20)))
    await ws.send_str(V.encode("speaker.join", id="u1", name="Маша", idx=1))
    await ws.send_bytes(V.pack_frame(V.KIND_MIC, 1, 0, 0, tone(20)))
    await env.until(lambda: env.engine.session("r1").stats.frames_in == 1)
    assert env.engine.session("r1").stats.bad_frames == 1
    await ws.send_str(V.encode("bye"))
    await env.until(lambda: env.engine.session("r1") is None)
    await http.close()


async def test_state_snapshot_lists_sessions(env):
    fake = env.adapter()
    await fake.start()
    await fake.join("u1", "Маша")
    await env.until(lambda: env.engine.session("s1") is not None and env.engine.session("s1").speakers)
    snap = env.engine.snapshot()
    assert snap["enabled"] and snap["sessions"][0]["id"] == "s1" and snap["sessions"][0]["speakers"][0]["name"] == "Маша"
    assert env.voice.connections == 1
    await fake.stop()
