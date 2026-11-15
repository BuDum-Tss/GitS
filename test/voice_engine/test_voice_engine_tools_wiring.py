"""Инструменты ядра voice.*, настройки движка и подключение к ядру (включение по адресам, выключенное состояние)."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from environments.base import ToolError, validate_args
from environments.registry import Registry
from services.voice_engine import events
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt
from services.voice_engine.testing import FakeClock, RecordingTransport
from services.voice_engine.tools import build_tools, register_voice_tools
from services.voice_engine.tts import FakeTts
from services.voice_engine.wiring import VoiceService


def tools_of(engine) -> dict:
    return {t.id: t for t in build_tools(engine)}


@pytest.fixture
async def engine():
    clock = FakeClock()
    eng = VoiceEngine(stt=FakeStt(), tts=FakeTts(ms_per_char=40), clock=clock)
    eng.test_clock = clock
    yield eng
    await eng.aclose()


async def open_session(engine, sid="s1", adapter="discord"):
    tr = RecordingTransport(engine.test_clock)
    sess = await engine.open_session(sid, adapter, tr, Capabilities(echo_free=True, can_barge_in=True))
    return sess, tr


async def test_say_queues_and_returns_immediately_with_uid(engine):
    sess, tr = await open_session(engine)
    out = await tools_of(engine)["voice.say"].call({"text": "привет всем"})
    assert "поставлено" in out and "uid u-" in out
    uid = out.split("uid ")[1].split(";")[0]
    assert sess.playback.current_uid == uid or sess.playback.waiting_uids() == [uid]      # вызов не ждал ни синтеза, ни звука
    await engine.test_clock.advance(0.1)
    assert tr.of("play.start")[0]["utterance_id"] == uid


async def test_say_session_choice(engine):
    with pytest.raises(ToolError) as err:
        await tools_of(engine)["voice.say"].call({"text": "алло"})
    assert err.value.code == "unavailable"
    a, _ = await open_session(engine, "a")
    b, _ = await open_session(engine, "b", "pc")
    with pytest.raises(ToolError) as err:
        await tools_of(engine)["voice.say"].call({"text": "алло"})
    assert err.value.code == "bad_request" and "a" in str(err.value) and "b" in str(err.value)
    out = await tools_of(engine)["voice.say"].call({"text": "алло", "session": "b"})
    assert "«b»" in out and b.playback.busy() and not a.playback.busy()
    with pytest.raises(ToolError):
        await tools_of(engine)["voice.say"].call({"text": "алло", "session": "нет"})
    with pytest.raises(ToolError):
        await tools_of(engine)["voice.say"].call({"text": "  ", "session": "a"})


async def test_say_replace_flag(engine):
    sess, _ = await open_session(engine)
    t = tools_of(engine)["voice.say"]
    await t.call({"text": "а" * 50})
    await engine.test_clock.advance(0.2)
    await t.call({"text": "второе"})
    first_waiting = sess.playback.waiting_uids()
    await t.call({"text": "срочное", "replace": True})
    assert first_waiting and first_waiting[0] not in sess.playback.waiting_uids()


async def test_sessions_tool_lists_channels_and_people(engine):
    t = tools_of(engine)["voice.sessions"]
    assert "нет" in await t.call({})
    sess, _ = await open_session(engine)
    engine.add_speaker("s1", "u1", "Маша", 1, is_owner=True)
    engine.add_speaker("s1", "u2", "Петя", 2)
    text = await t.call({})
    assert "s1 (discord)" in text and "Маша (владелец)" in text and "Петя" in text and "молчит" in text
    engine.speak("s1", "привет")
    await engine.test_clock.advance(0.1)
    assert "говорит" in await t.call({}) and sess.id == "s1"


async def test_stop_and_leave_tools(engine):
    sess, tr = await open_session(engine)
    tools = tools_of(engine)
    engine.speak("s1", "а" * 50)
    await engine.test_clock.advance(0.2)
    assert "остановлена" in await tools["voice.stop"].call({})
    await engine.test_clock.settle()
    assert tr.of("play.stop")[0]["reason"] == "stop"
    assert "выйти" in await tools["voice.leave"].call({})
    assert tr.of("leave")
    tr.fail = True
    with pytest.raises(ToolError):
        await tools["voice.leave"].call({})
    assert sess.id == "s1"


async def test_tool_descriptions_and_schema_are_valid(engine):
    for tool in build_tools(engine):
        assert tool.id.startswith("voice.") and tool.risk == "auto" and tool.availability == "always" and not tool.sends
        assert tool.description and len(tool.description) < 260
    args, err = validate_args(tools_of(engine)["voice.say"], {"text": "привет", "replace": "да"})
    assert err is None and args == {"text": "привет", "replace": True}
    assert validate_args(tools_of(engine)["voice.say"], {})[1] is not None


async def test_register_into_registry(engine):
    reg = Registry()
    env = register_voice_tools(reg, engine)
    assert reg.envs[env.id] is env and {t.id for t in env.tools} == {"voice.say", "voice.sessions", "voice.stop", "voice.leave", "voice.transcript"}


# ── настройки и подключение ──────────────────────────────────────────────────────

def test_config_enabled_only_with_both_urls():
    assert not EngineConfig.from_env({}).enabled
    assert not EngineConfig.from_env({"GITS_STT_URL": "http://stt:8000"}).enabled
    cfg = EngineConfig.from_env({"GITS_STT_URL": "http://stt:8000", "GITS_TTS_URL": "http://tts:8771"})
    assert cfg.enabled and cfg.stt_url == "http://stt:8000" and cfg.barge_in == "auto"
    assert EngineConfig.from_env({"STT_URL": "http://s", "ESPEECH_URL": "http://e"}).enabled
    off = EngineConfig.from_env({"GITS_STT_URL": "http://s", "GITS_TTS_URL": "http://t", "GITS_VOICE_ENGINE": "0"})
    assert not off.enabled and "GITS_VOICE_ENGINE" in off.disabled_reason
    forced = EngineConfig.from_env({"GITS_VOICE_ENGINE": "1"})
    assert not forced.enabled and "адреса" in forced.disabled_reason


def test_config_clamps_bad_values():
    cfg = EngineConfig.from_env({"GITS_VOICE_BARGE_IN_MS": "5", "GITS_VOICE_THRESHOLD_DB": "мусор", "GITS_VOICE_BARGE_IN": "всех",
                                 "GITS_TTS_SPEED": "9", "GITS_VOICE_VAD": "strange"})
    assert cfg.barge_in_ms == 100 and cfg.energy_threshold_db == -42.0 and cfg.barge_in == "auto"
    assert cfg.tts_speed == 1.5 and cfg.vad == "energy"
    assert set(cfg.public()) >= {"enabled", "stt_url", "barge_in"}


def build(environ):
    submitted = []

    async def submit(peer, event):
        submitted.append((peer, event))

    svc = VoiceService.build(environ, submit=submit, token=lambda: "tok", allow=lambda remote: True,
                             names=lambda: ("Глэдос",), journal=lambda *a: None)
    return svc, submitted


async def test_disabled_service_registers_nothing_but_state():
    svc, _ = build({})
    assert not svc.enabled
    reg = Registry()
    svc.register_tools(reg)
    assert "voicechat" not in reg.envs
    app = web.Application()
    svc.attach(app)
    paths = {r.resource.canonical for r in app.router.routes() if hasattr(r.resource, "canonical")}
    assert "/api/voice" in paths and "/voice/ws" not in paths
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/voice")).json()
    assert body["enabled"] is False and "адреса" in body["reason"]
    await svc.stop()


async def test_enabled_service_routes_tools_and_events():
    svc, submitted = build({"GITS_STT_URL": "http://127.0.0.1:1", "GITS_TTS_URL": "http://127.0.0.1:2"})
    assert svc.enabled
    reg = Registry()
    svc.register_tools(reg)
    assert "voicechat" in reg.envs
    app = web.Application()
    svc.attach(app)
    paths = {r.resource.canonical for r in app.router.routes() if hasattr(r.resource, "canonical")}
    assert {"/api/voice", "/voice/ws"} <= paths
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/voice")).json()
    assert body["enabled"] is True and body["sessions"] == [] and body["connections"] == 0
    await svc.engine._emit_event({"kind": events.KIND_UTTERANCE})
    assert submitted == [(events.PEER_ID, {"kind": events.KIND_UTTERANCE})]
    await svc.stop()


def test_name_patterns():
    pats = events.name_patterns(["Глэдос", "ГЛаДОС", "Э", ""])
    assert events.is_addressed("Глэдос, привет", pats) and events.is_addressed("скажи глэдосу", pats)
    assert not events.is_addressed("привет, мир", pats) and not events.is_addressed("глэдосовщина", pats)
    assert events.is_addressed("Ёлка зовёт", events.name_patterns(["Елка"]))
    ev = events.utterance_event(text="т" * 2000, speaker={"id": "u", "is_owner": False}, session="s", adapter="a", duration_ms=5,
                                language="ru", addressed=False, reason="")
    assert len(ev["data"]["phrase"]) == events.EVENT_TEXT_MAX and ev["sender"]["name"] == "u"


def test_wiring_module_does_not_pull_heavy_imports():
    """Выключенный движок не добавляет ядру зависимостей: numpy и клиенты моделей подгружаются, только когда он включён."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    code = ("import sys; sys.path[:0] = [r'%s', r'%s']; import services.voice_engine.wiring as w; "
            "svc = w.VoiceService.build({}, submit=None, token=None, allow=None, names=None, journal=None); "
            "assert not svc.enabled; heavy = [m for m in ('numpy', 'services.voice_engine.engine', 'services.voice_engine.tts') "
            "if m in sys.modules]; assert not heavy, heavy" % (root / "agent", root / "agent" / "interfaces" / "workspace"))
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


async def test_wire_voice_for_core_uses_bus_token_and_registers_tools():
    from types import SimpleNamespace

    from services.voice_engine.wiring import wire_voice
    submitted = []

    async def submit(peer, event):
        submitted.append((peer, event))

    layer = SimpleNamespace(attention=SimpleNamespace(submit=submit), hub=SimpleNamespace(token="шина", allow=[]))
    reg = Registry()
    off = wire_voice(layer, reg, {})
    assert not off.enabled and "voicechat" not in reg.envs
    on = wire_voice(layer, reg, {"GITS_STT_URL": "http://127.0.0.1:1", "GITS_TTS_URL": "http://127.0.0.1:2"})
    assert on.enabled and "voicechat" in reg.envs
    assert on.server._token() == "шина" and on.server._allow("127.0.0.1") and not on.server._allow("10.1.2.3")
    names = on.engine._names()
    assert names and names[0]                                      # имя и алиасы активного персонажа
    await on.stop()
