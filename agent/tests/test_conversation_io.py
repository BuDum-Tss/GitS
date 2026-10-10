"""Каналы разговора и вывод речи (ТЗ 02, CORE-24…CORE-28) на настоящем Companion с заглушками речи и модуля-канала."""
import asyncio
import json

from hub_helpers import RawModule, manifest, running_core

import events_log
import settings
import speech_out
from companion import Companion
from conversation_io import DASHBOARD, VOICE, Channel, ConversationIO, UserInput, current_channel


class RecordingSpeech:
    """SpeechOutput-заглушка: запоминает, что должно было прозвучать."""

    def __init__(self):
        self.spoken: list[tuple] = []
        self.speaking = False

    async def speak(self, utterance_id, text, emotion, priority):
        self.spoken.append((utterance_id, text, emotion, priority))

    async def stop(self, utterance_id=None):
        self.spoken.append(("stop", utterance_id))

    def is_speaking(self):
        return self.speaking

    def current_segment(self):
        return ""


def make_companion(tmp_path, **kw):
    mem = tmp_path / "memory"
    speech = RecordingSpeech()
    c = Companion(str(mem), rel=None, speech=speech, **kw)
    return c, speech


async def test_dashboard_and_voice_replies_are_spoken(tmp_path):
    c, speech = make_companion(tmp_path)
    handled = []

    async def fake_handle(text):
        handled.append((text, current_channel.get()))
        await c.say("Ответ")
    c._handle_text = fake_handle
    await c.on_user_message("привет")                               # чат дашборда / строка питомца
    await c.on_voice("слушай", {"owner": True, "has_owner": True})  # голос
    assert [h[1] for h in handled] == [DASHBOARD, VOICE]
    assert [s[1] for s in speech.spoken] == ["Ответ", "Ответ"]
    assert speech.spoken[0][0].startswith("u-") and len(speech.spoken[0][0]) == 10     # CORE-28: u- + 8 hex
    assert current_channel.get() is None                            # контекст обращения снят


async def test_self_initiated_say_goes_to_speech_and_dialogue(tmp_path):
    c, speech = make_companion(tmp_path)
    await c.say("Сама заговорила", "happy", priority=40)
    assert speech.spoken[0][1:] == ("Сама заговорила", "happy", 40)
    assert c._mem.recent_dialogue(1)[0]["text"] == "Сама заговорила"       # чат дашборда
    assert c.presence.state["last_say"] == "Сама заговорила" and c.presence.state["emotion"] == "happy"


async def test_module_channel_reply_is_delivered_not_spoken(tmp_path):
    async with running_core(tmp_path) as core:
        c, speech = make_companion(tmp_path)
        c.io = ConversationIO(c, core.hub)
        chan = RawModule(core, manifest("chat", capabilities=["chat.channel"]))
        delivered = []
        chan.handlers["deliver"] = lambda body: delivered.append(body) or {"delivered": True}
        try:
            await chan.hello()

            async def fake_handle(text):
                await c.say("Ответ в телегу")
            c._handle_text = fake_handle
            await c.io.from_module("Привет", {"module": "chat", "target": "tg:42"}, {"id": "tg:42", "is_owner": True})
            assert delivered == [{"target": "tg:42", "text": "Ответ в телегу"}]            # CORE-25
            assert speech.spoken == []                                                    # вслух не произносится
            assert c._mem.recent_dialogue(1)[0]["text"] == "Ответ в телегу"
        finally:
            await chan.close()


async def test_deliver_failure_or_offline_module_keeps_reply_in_dashboard_chat(tmp_path):
    async with running_core(tmp_path) as core:
        c, speech = make_companion(tmp_path)
        c.io = ConversationIO(c, core.hub)
        chan = RawModule(core, manifest("chat", capabilities=["chat.channel"]))
        chan.handlers["deliver"] = lambda body: {"delivered": False}
        await chan.hello()

        async def fake_handle(text):
            await c.say("Не дошло")
        c._handle_text = fake_handle
        try:
            await c.io.from_module("Привет", {"module": "chat", "target": "tg:42"})
            assert speech.spoken == [] and c._mem.recent_dialogue(1)[0]["text"] == "Не дошло"
            assert any("не доставлен" in e["text"] for e in events_log.tail(20, {"module"}))
        finally:
            await chan.close()
        await core.wait_for(lambda: not core.hub.is_connected("chat"))
        await c.io.from_module("Ещё раз", {"module": "chat", "target": "tg:42"})            # модуль офлайн
        assert speech.spoken == [] and c._mem.recent_dialogue(1)[0]["text"] == "Не дошло"
        assert sum("не доставлен" in e["text"] for e in events_log.tail(20, {"module"})) == 2
        # модуль без chat.channel доставку не принимает
        plain = RawModule(core, manifest("plain"))
        plain.handlers["deliver"] = lambda body: {"delivered": True}
        await plain.hello()
        try:
            assert not await c.io.deliver(Channel("module", "plain", "x"), "текст")
        finally:
            await plain.close()


async def test_user_input_defaults_and_channel_dict():
    ui = UserInput("текст")
    assert ui.channel == DASHBOARD and ui.sender is None and ui.ts > 0
    assert Channel("module", "tg", "tg:1").to_dict() == {"kind": "module", "module": "tg", "target": "tg:1"}
    assert VOICE.to_dict() == {"kind": "voice", "module": None, "target": None}


# ── вывод речи и присутствие ──────────────────────────────────────────────────────

async def test_legacy_pet_speech_writes_say_json_and_tracks_speaking(tmp_path):
    seen = []
    presence = speech_out.PresenceTracker(listener=lambda snap: seen.append(snap))
    t = [1000.0]
    sp = speech_out.LegacyPetSpeech(tmp_path, presence, now=lambda: t[0])
    await sp.speak("u-00000001", "Привет, подопытный.", "happy", 50)
    say = json.loads((tmp_path / "say.json").read_text(encoding="utf-8"))
    assert say["text"] == "Привет, подопытный." and say["id"] == 1 and say["ts"] == 1000.0
    assert sp.is_speaking() and sp.current_segment() == "Привет, подопытный."
    assert presence.state["speaking"] and presence.state["utterance_id"] == "u-00000001" and presence.state["emotion"] == "happy"
    t[0] += len("Привет, подопытный.") * 0.075 + 1.5 + 0.01                  # по длине текста
    assert not sp.is_speaking() and sp.current_segment() == ""
    await sp.speak("u-00000002", "Вторая", None, 50)
    await sp.stop("u-00000001")                                            # чужая реплика: не трогаем
    assert sp.is_speaking()
    await sp.stop()
    assert not sp.is_speaking() and presence.state["speaking"] is False and presence.state["utterance_id"] is None
    assert len(seen) >= 3


async def test_legacy_speech_end_is_announced_by_timer(tmp_path):
    presence = speech_out.PresenceTracker()
    sp = speech_out.LegacyPetSpeech(tmp_path, presence)
    sp.PAD_S, sp.SECONDS_PER_CHAR = 0.05, 0.0
    await sp.speak("u-1", "коротко", None, 50)
    assert presence.state["speaking"]
    await asyncio.sleep(0.2)
    assert presence.state["speaking"] is False


async def test_muted_voice_writes_text_but_is_not_speaking(tmp_path):
    settings.update(voice_muted=True)
    sp = speech_out.LegacyPetSpeech(tmp_path)
    await sp.speak("u-1", "тихо", None, 50)
    assert (tmp_path / "say.json").exists() and not sp.is_speaking()


async def test_presence_updates_only_on_change_and_unknown_fields_ignored():
    calls = []
    p = speech_out.PresenceTracker(listener=calls.append)
    assert p.update(busy=True) and not p.update(busy=True) and not p.update(nonsense=1)
    assert len(calls) == 1 and set(calls[0]) == set(speech_out.PresenceTracker.DEFAULTS)


async def test_companion_busy_presence_and_speaking_follow_input(tmp_path):
    c, speech = make_companion(tmp_path)
    states = []

    async def fake_handle(text):
        states.append(c.presence.state["busy"])
    c._handle_text = fake_handle
    await c.on_user_message("раз")
    assert states == [True] and c.presence.state["busy"] is False
    speech.speaking = True
    assert c.is_speaking() is True
