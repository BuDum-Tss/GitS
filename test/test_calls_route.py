"""Маршрут обычного текста хода агента в голосовые каналы (канал `calls`): правило выбора по пачке событий, выход
`Companion.voice_out`, очередь речи и строка голосового события. Модель, сеть и звук подменены."""
from services.attention_v2.event import BACKGROUND, NORMAL, USER, Event, Reply
from services.attention_v2.render import render_event
from services.conversation.conversation_io import CALLS, CALLS_AUTO, Channel, current_channel
from services.runtime.turns import reply_channel_for
from test_companion_attention import Rig


def ev(kind="discord.message", ws="discord", prio=NORMAL, reply=None, ts=1_700_000_000.0, **kw):
    return Event(workspace=ws, kind=kind, text="привет", ts=ts, priority=prio, reply=reply, **kw)


def voice_event(session="s1", tool="discord.say", prio=USER, **kw):
    return ev("voicechat.utterance", "voicechat", prio, Reply(tool, session), **kw)


# ---- правило маршрута ---------------------------------------------------------------------------------------------------

def test_voice_phrase_goes_to_all_open_sessions():
    assert reply_channel_for([voice_event("s7")]) == CALLS
    assert reply_channel_for([voice_event("s7", tool="voice.say")]) == CALLS


def test_voice_phrase_without_target_goes_to_all_open_sessions():
    e = ev("voicechat.utterance", "voicechat", USER, Reply("voice.say"))
    assert reply_channel_for([e]) == CALLS
    e2 = ev("voicechat.utterance", "voicechat", USER, Reply("voice.say"), data={"session": "s3"})
    assert reply_channel_for([e2]) == CALLS


def test_dashboard_chat_speaks_to_all_calls():
    chat = ev("dashboard.text", "dashboard", USER, Reply("dashboard.text"))
    assert reply_channel_for([chat]) == CALLS


def test_autonomous_turns_speak_to_all_calls():
    assert reply_channel_for([ev("core.work_tick", "core", NORMAL)]) == CALLS_AUTO
    assert reply_channel_for([ev("rimworld.raid", "rimworld", 70)]) == CALLS_AUTO
    assert reply_channel_for([ev("core.timer", "core", 30)]) == CALLS_AUTO
    assert reply_channel_for([]) == CALLS_AUTO


def test_mixed_batch_prefers_human_voice_over_game_events():
    batch = [ev("rimworld.raid", "rimworld", 70), voice_event("s2"), ev("core.work_tick", "core", NORMAL)]
    assert reply_channel_for(batch) == CALLS


def test_most_prioritized_user_event_decides():
    chat = ev("dashboard.text", "dashboard", USER, Reply("dashboard.text"))
    voice = voice_event("s2", prio=USER + 5)
    assert reply_channel_for([chat, voice]) == CALLS
    voice_low = voice_event("s2", prio=USER)
    chat_high = ev("dashboard.text", "dashboard", USER + 5, Reply("dashboard.text"))
    assert reply_channel_for([voice_low, chat_high]) == CALLS


def test_background_voice_is_not_a_human_address():
    assert reply_channel_for([voice_event("s2", prio=BACKGROUND)]) == CALLS_AUTO


def test_labels():
    assert CALLS.label() == "calls" and CALLS_AUTO.label() == "calls:auto" and Channel("calls", target="s1").label() == "calls:s1"


# ---- Companion._say_now с каналом calls -------------------------------------------------------------------------------

class FakeVoiceOut:
    def __init__(self, sessions, broken=()):
        self.sessions, self.broken, self.said = list(sessions), set(broken), []

    def open_sessions(self):
        return list(self.sessions)

    def speak(self, session_id, text):
        if session_id in self.broken:
            raise KeyError(session_id)
        self.said.append((session_id, text))


async def say_in(rig, channel, text="Привет, как дела"):
    token = current_channel.set(channel)
    try:
        await rig.companion._say_now(text, "happy")
    finally:
        current_channel.reset(token)


def watch_record(rig):
    calls = []
    orig = rig.companion._record_say
    rig.companion._record_say = lambda text, channel: (calls.append((text, channel)), orig(text, channel))[1]
    return calls


async def test_auto_calls_speak_to_every_open_session(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, attention=False)
    rig.companion.voice_out = FakeVoiceOut(["a", "b"])
    recorded = watch_record(rig)
    await say_in(rig, CALLS_AUTO)
    assert rig.companion.voice_out.said == [("a", "Привет, как дела"), ("b", "Привет, как дела")]
    assert rig.speech.spoken == []                                   # колонки ПК молчат
    assert recorded == [("Привет, как дела", CALLS_AUTO)]
    assert rig.companion.presence.snapshot()["sticker"] == "happy"


async def test_calls_without_sessions_fall_back_to_pc_speakers(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, attention=False)
    rig.companion.voice_out = FakeVoiceOut([])
    await say_in(rig, CALLS_AUTO)
    assert [t[1] for t in rig.speech.spoken] == ["Привет, как дела"]
    rig2 = Rig(tmp_path / "2", monkeypatch, attention=False)         # выхода нет совсем (движок голоса выключен)
    await say_in(rig2, CALLS_AUTO)
    assert [t[1] for t in rig2.speech.spoken] == ["Привет, как дела"]


async def test_targeted_calls_speak_only_to_that_session(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, attention=False)
    rig.companion.voice_out = FakeVoiceOut(["a", "b"])
    await say_in(rig, Channel("calls", target="b"))
    assert rig.companion.voice_out.said == [("b", "Привет, как дела")]
    await say_in(rig, Channel("calls", target="gone"))               # сессия закрылась: во все открытые
    assert [s for s, _ in rig.companion.voice_out.said[1:]] == ["a", "b"]
    assert rig.speech.spoken == []


async def test_failed_session_does_not_stop_the_others(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, attention=False)
    rig.companion.voice_out = FakeVoiceOut(["a", "b"], broken={"a"})
    await say_in(rig, CALLS_AUTO)
    assert rig.companion.voice_out.said == [("b", "Привет, как дела")] and rig.speech.spoken == []


async def test_all_sessions_failed_fall_back_to_pc_speakers(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch, attention=False)
    rig.companion.voice_out = FakeVoiceOut(["a"], broken={"a"})
    await say_in(rig, CALLS_AUTO)
    assert len(rig.speech.spoken) == 1


# ---- очередь речи -----------------------------------------------------------------------------------------------------

async def test_auto_channel_is_proactive_and_reaches_playback(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.companion.voice_out = FakeVoiceOut(["a", "b"])
    token = current_channel.set(CALLS_AUTO)
    try:
        auto = rig.att.enqueue_say("Сама говорю", None, None)
        fact = rig.att.enqueue_fact("итог", "")
    finally:
        current_channel.reset(token)
    assert (auto.source, auto.priority, auto.channel) == ("proactive", 30, CALLS_AUTO)
    assert (fact.source, fact.channel) == ("proactive", CALLS_AUTO)
    token = current_channel.set(Channel("calls", target="a"))
    try:
        direct = rig.att.enqueue_say("Тебе", None, None)
    finally:
        current_channel.reset(token)
    assert (direct.source, direct.priority) == ("conversation", 95)
    rig.att.speech.items.remove(fact)
    await rig.att.coordinator.play(auto)                                   # воспроизведение ставит канал кандидата
    await rig.att.coordinator.play(direct)
    assert rig.companion.voice_out.said == [("a", "Сама говорю"), ("b", "Сама говорю"), ("a", "Тебе")]
    assert rig.speech.spoken == []


# ---- строка события ---------------------------------------------------------------------------------------------------

def test_voice_event_line_says_text_reply():
    assert render_event(voice_event()).endswith("{ответ: текстом}")
    assert render_event(voice_event(tool="voice.say")).endswith("{ответ: текстом}")
    chat = ev("dashboard.text", "dashboard", USER, Reply("dashboard.text"))
    assert render_event(chat).endswith("{ответ: dashboard.text}")
    assert render_event(ev(reply=Reply("discord.text", "c"))).endswith("{ответ: discord.text}")


# ---- Telegram и Discord: ответ текстом в тот же чат -------------------------------------------------------------------

def test_telegram_message_gets_text_reply_in_same_chat():
    e = ev("telegram.dm", "telegram", USER, Reply("telegram.text", target="user:42#7"))
    assert reply_channel_for([e]) == Channel("module", module="telegram", target="user:42#7")
    assert render_event(e).endswith("{ответ: текстом}")


def test_discord_text_message_gets_text_reply_in_same_channel():
    e = ev("discord.message", "discord", USER, Reply("discord.text", target="channel:5"))
    assert reply_channel_for([e]) == Channel("module", module="discord", target="channel:5")


def test_chat_message_without_address_or_below_user_keeps_old_route():
    no_target = ev("telegram.dm", "telegram", USER, Reply("telegram.text"))
    assert reply_channel_for([no_target]) is None
    assert render_event(no_target).endswith("{ответ: telegram.text}")
    below = ev("telegram.dm", "telegram", NORMAL, Reply("telegram.text", target="user:1"))
    assert reply_channel_for([below]) == CALLS_AUTO
    assert render_event(below).endswith("{ответ: telegram.text}")
