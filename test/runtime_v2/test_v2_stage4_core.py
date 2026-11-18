"""Этап 4 (часть ядра): core.timer, core.event_log, голосовые события для v2, discord.say. Без модели, сети и звука."""
from types import SimpleNamespace

import pytest

from environments.base import ToolError
from environments.registry import Registry
from services.attention_v2.attention import AttentionV2
from services.attention_v2.event import Event, parse_priority
from services.attention_v2.eventlog import EventLog
from services.attention_v2.kinds import KindRegistry, from_legacy
from services.attention_v2.routing import register_core_kinds, timer_event
from services.attention_v2.testing import FakeClock, FakeRuntime
from services.reflection.timer import TEXT_PREFIX, ImpulseTimer
from services.runtime import attention_wiring as aw
from services.runtime.attention_wiring import event_log_report, register_v2_tools
from services.voice_engine import events as vevents
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt
from services.voice_engine.testing import FakeClock as VoiceClock
from services.voice_engine.testing import RecordingTransport
from services.voice_engine.tools import build_tools
from services.voice_engine.tts import FakeTts


class Rig:
    def __init__(self):
        self.clock = FakeClock()
        self.registry = KindRegistry()
        register_core_kinds(self.registry)
        self.log = EventLog(clock=self.clock)
        self.runtime = FakeRuntime(clock=self.clock)
        self.v2 = AttentionV2(self.registry, self.log, self.runtime, self.clock, lambda: "discord")
        self.runtime.attention = self.v2


# ---- core.timer ----------------------------------------------------------------------------------------------------------

class Store:
    def __init__(self, recs):
        self.recs, self.status = recs, {}

    def add(self, *a, **k): ...

    def pending(self):
        return [r for r in self.recs if r["id"] not in self.status]

    def set_status(self, rid, st):
        self.status[rid] = st


def _rec(priority=None):
    src = {"kind": "conversation"}
    if priority is not None:
        src["priority"] = priority
    return {"id": "r1", "due": 100.0, "text": "проверить сборку", "source": src, "depth": 0, "untrusted": False, "ttl_s": None}


async def _fire(rec):
    out = []

    async def emit(body):
        out.append(body)
    await ImpulseTimer(Store([rec]), emit, clock=lambda: 100.0).fire_due()
    return out


async def _timer_emit(rig, body):
    """Тот же перевод, что в `timer_emit` ядра, но без сборки всего слоя."""
    text = body["text"].removeprefix(TEXT_PREFIX)
    await rig.v2.process_event(timer_event(text, priority=parse_priority(body["data"].get("priority")), ts=rig.clock.time()))


async def test_timer_arrives_as_core_timer_event_notable_by_default():
    rig = Rig()
    (body,) = await _fire(_rec())
    assert body["kind"] == "self.reminder"                          # тело для старого пути то же
    await _timer_emit(rig, body)
    (e,) = rig.runtime.last_trigger
    assert (e.workspace, e.kind, e.priority) == ("core", "core.timer", 50)
    assert e.text == "напоминание: проверить сборку" and e.expires_at == e.ts + 600


async def test_timer_priority_is_clamped_by_kind_ceiling():
    rig = Rig()
    (body,) = await _fire(_rec("USER"))
    await _timer_emit(rig, body)
    assert rig.runtime.last_trigger[0].priority == 70


async def test_timer_background_priority_does_not_wake():
    rig = Rig()
    (body,) = await _fire(_rec(10))
    await _timer_emit(rig, body)
    assert not rig.runtime.triggers


async def test_wired_timer_emit_goes_through_process_event(monkeypatch):
    seen = []

    class V2Stub:
        log = None

        async def process_event(self, e):
            seen.append(e)

    monkeypatch.setattr(aw, "AttentionV2", lambda *a, **k: V2Stub())
    monkeypatch.setattr(aw, "ResidentRuntime", lambda *a, **k: SimpleNamespace())
    layer = aw.wire_attention_v2(SimpleNamespace(resident=None), SimpleNamespace(), SimpleNamespace(manifest_of=lambda p: None),
                                 SimpleNamespace(current="x"), SimpleNamespace())
    await layer.timer_emit({"text": TEXT_PREFIX + "a; b", "data": {"priority": 70}})
    assert seen[0].kind == "core.timer" and seen[0].text == "напоминание: a; b" and seen[0].priority == 70


def test_old_timer_path_without_v2_is_unchanged():
    ev = ImpulseTimer._event([_rec(70)], 100.0, late=False)
    assert ev["kind"] == "self.reminder" and ev["text"].startswith("Напоминание, которое ты назначила себе:") and ev["urgency_hint"] == 2


# ---- core.event_log ------------------------------------------------------------------------------------------------------

async def test_event_log_tool_lists_short_lines_without_data():
    rig = Rig()
    await rig.v2.process_event(Event(workspace="core", kind="core.timer", text="напоминание: " + "я" * 300, ts=rig.clock.time(),
                                     data={"секрет": "не показывать"}))
    out = event_log_report(rig.log, {})
    assert "core/core.timer" in out and "секрет" not in out and "я" * 161 not in out
    assert "delivered" in out or "queued" in out
    assert "ничего" in event_log_report(rig.log, {"workspace": "rimworld"})
    assert "core.timer" in event_log_report(rig.log, {"query": "НАПОМИНАНИЕ", "kind": "core.timer", "since": 60}, now=rig.clock.time())
    assert "ничего" in event_log_report(rig.log, {"since": 5}, now=rig.clock.time() + 100)
    assert event_log_report(rig.log, {"limit": 1000}).count("\n") <= 100


def test_event_log_tool_is_read_only_and_reports_disabled_without_v2():
    env = register_v2_tools(Registry(), None)
    tool = env.tools[0]
    assert tool.id == "core.event_log" and tool.read_only
    assert "выключен" in event_log_report(None, {})
    with pytest.raises(ToolError):
        event_log_report(EventLog(), {"limit": "много"})


# ---- события голоса для v2 -----------------------------------------------------------------------------------------------

def _utt(reason, addressed, adapter="discord", speaker="u1"):
    return vevents.utterance_event(text="привет", speaker={"id": speaker, "name": "Маша"}, session="s1", adapter=adapter,
                                   duration_ms=500, language="ru", addressed=addressed, reason=reason, urgency_hint=2)


@pytest.mark.parametrize("reason,addressed,want", [("name", True, 100), ("owner", True, 100), ("one_to_one", True, 100),
                                                   ("continuation", True, 70), ("", False, 10)])
def test_voice_event_priority_by_reason(reason, addressed, want):
    reg = KindRegistry()
    register_core_kinds(reg)
    e = from_legacy("voicechat", _utt(reason, addressed), reg)
    assert reg.normalize(e).accepted
    assert e.priority == want and e.group_key == "s1:u1" and e.expires_at == e.ts + 90


def test_voice_event_reply_tool_by_adapter_and_legacy_format_kept():
    reg = KindRegistry()
    register_core_kinds(reg)
    assert from_legacy("voicechat", _utt("name", True), reg).reply.tool == "discord.say"
    other = from_legacy("voicechat", _utt("name", True, adapter="pc"), reg)
    assert other.reply.tool == "voice.say" and other.reply.target == "s1"
    legacy = _utt("name", True)
    assert legacy["reply_channel"]["tool"] == "voice.say" and "dedupe_key" not in legacy and legacy["addressed_to_me"] is True


async def test_same_speaker_phrases_merge_by_group_key():
    rig = Rig()
    for text in ("раз", "два"):
        ev = _utt("", False)
        ev["text"] = text
        await rig.v2.process_event(from_legacy("voicechat", ev, rig.registry, now=rig.clock.time()))
    assert rig.v2.status()["merged"] == 1 and len(rig.v2.queue) == 1


async def test_spoken_is_background_and_never_wakes_even_after_on_turn_timer():
    rig = Rig()
    ev = vevents.spoken_event(session="s1", adapter="discord", uid="u-1", spoken_text="привет", completed=True)
    ev["urgency_hint"] = 5                                         # даже если отправитель крикнет, потолок BACKGROUND
    ev.pop("v2")
    e = from_legacy("voicechat", ev, rig.registry, now=rig.clock.time())
    await rig.v2.process_event(e)
    assert e.priority == 10 and e.expires_at == e.ts + 120
    await rig.clock.advance(60)
    assert not rig.runtime.triggers and rig.v2.status()["launches"] == 0
    other = from_legacy("voicechat", _utt("name", True), rig.registry, now=rig.clock.time())
    await rig.v2.process_event(other)
    await rig.clock.advance(1)
    assert e in rig.runtime.last_trigger                         # фон ушёл в пачку вместе с важным


# ---- discord.say ---------------------------------------------------------------------------------------------------------

@pytest.fixture
async def engine():
    clock = VoiceClock()
    eng = VoiceEngine(stt=FakeStt(), tts=FakeTts(ms_per_char=40), clock=clock)
    eng.test_clock = clock
    yield eng
    await eng.aclose()


async def _open(engine, sid, adapter):
    return await engine.open_session(sid, adapter, RecordingTransport(engine.test_clock), Capabilities(echo_free=True, can_barge_in=True))


async def test_discord_say_queues_and_omits_session_with_single_discord_channel(engine):
    say = {t.id: t for t in build_tools(engine)}["discord.say"]
    with pytest.raises(ToolError):
        await say.call({"text": "алло"})
    pc = await _open(engine, "pc1", "pc")
    with pytest.raises(ToolError):                                  # канал не discord
        await say.call({"text": "алло"})
    dc = await _open(engine, "dc1", "discord")
    out = await say.call({"text": "привет всем"})
    assert "поставлено" in out and "«dc1»" in out and dc.playback.busy() and not pc.playback.busy()
    await _open(engine, "dc2", "discord")
    with pytest.raises(ToolError) as err:
        await say.call({"text": "алло"})
    assert err.value.code == "bad_request"
    with pytest.raises(ToolError):
        await say.call({"text": "алло", "session": "pc1"})
    assert "«dc2»" in await say.call({"text": "алло", "session": "dc2", "replace": True})


@pytest.mark.parametrize("flag,expect", [(False, 0), (True, 1)])
async def test_engine_emits_spoken_event_only_when_asked(engine, flag, expect):
    got = []

    async def emit(ev):
        got.append(ev)
    eng = VoiceEngine(stt=FakeStt(), tts=FakeTts(ms_per_char=10), clock=engine.test_clock, emit_event=emit, emit_spoken=flag)
    tr = RecordingTransport(engine.test_clock)
    await eng.open_session("d", "discord", tr, Capabilities(echo_free=True, can_barge_in=True))
    eng.speak("d", "привет")
    await engine.test_clock.advance(3)
    await eng.settle()
    assert [e["kind"] for e in got].count("voicechat.spoken") == expect
    await eng.aclose()
