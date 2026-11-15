"""Скорость реакции в созвоне: обращённость (имя, владелец, продолжение), контекст в тексте события, метрики задержки,
раннее первое предложение, срок жизни ожидающих реплик."""
import pytest

from gits_sdk.voice.testing import silence, tone
from services.voice_engine import events
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.sentences import split_sentences
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt, Transcript
from services.voice_engine.testing import ClockTts, FakeClock, RecordingTransport
from services.voice_engine.tools import build_tools


class ClockStt(FakeStt):
    """Распознавание, которое занимает время на поддельных часах."""

    def __init__(self, clock, texts, latency_s=0.3):
        super().__init__(texts)
        self.clock, self.latency_s = clock, latency_s

    async def transcribe(self, pcm, *, lang=None):
        await self.clock.sleep(self.latency_s)
        return Transcript(text=self._texts.popleft() if self._texts else "тест", language="ru", duration_ms=500)


class Rig:
    def __init__(self, texts, cfg=None, stt_latency=0.0, **tts_kw):
        self.clock = FakeClock()
        self.events: list[dict] = []
        self.speech: list[dict] = []
        self.stt = ClockStt(self.clock, texts, stt_latency)
        self.tts = ClockTts(self.clock, latency_s=0.1, **tts_kw)

        async def emit(e):
            self.events.append(e)

        self.engine = VoiceEngine(stt=self.stt, tts=self.tts, config=cfg or EngineConfig(), clock=self.clock, emit_event=emit,
                                  on_speech=self.speech.append, names=lambda: ("Глэдос",))
        self.transport = RecordingTransport(self.clock)

    async def open(self, title="Игры"):
        self.sess = await self.engine.open_session("s1", "fake", self.transport, Capabilities(echo_free=True, can_barge_in=True),
                                                   title=title)
        self.engine.add_speaker("s1", "owner", "Хозяин", 1, is_owner=True)
        self.engine.add_speaker("s1", "u2", "Петя", 2)
        self.engine.add_speaker("s1", "u3", "Вася", 3)
        return self

    async def say(self, who, ms=600, wait=0.0):
        self.engine.feed("s1", who, tone(ms) + silence(800))
        await self.clock.advance(wait or 0.001)
        await self.engine.settle()

    def last(self):
        return self.events[-1]


@pytest.fixture
async def rig():
    r = await Rig(["фраза"] * 20).open()
    yield r
    await r.engine.aclose()


# ── обращённость ────────────────────────────────────────────────────────────────

async def test_owner_without_name_is_addressed_in_crowded_channel():
    r = await Rig(["Расскажи анекдот."]).open()
    await r.say("owner")
    ev = r.last()
    assert ev["addressed_to_me"] is True and ev["data"]["addressed_reason"] == "owner" and ev["data"]["needs_reply"] is True
    assert "urgency_hint" not in ev
    await r.engine.aclose()


async def test_owner_addressing_can_be_switched_off():
    r = await Rig(["Расскажи анекдот."], EngineConfig(owner_addressed=False)).open()
    await r.say("owner")
    assert r.last()["addressed_to_me"] is False
    await r.engine.aclose()


async def test_other_person_without_name_is_ambient_with_urgency_2():
    r = await Rig(["что-то про игру", "ещё про игру"]).open()
    await r.say("u2")
    ev = r.last()
    assert ev["addressed_to_me"] is False and ev["urgency_hint"] == 2 and ev["data"]["needs_reply"] is False
    await r.engine.aclose()
    r = await Rig(["что-то про игру"], EngineConfig(ambient_urgency=3)).open()
    await r.say("u2")
    assert r.last()["urgency_hint"] == 3
    await r.engine.aclose()


async def test_bot_speaker_is_never_addressed_by_owner_flag_or_continuation():
    r = await Rig(["бот говорит"]).open()
    r.engine.add_speaker("s1", "bot", "Бот", 9, is_owner=True, is_bot=True)
    await r.say("bot")
    assert r.last()["addressed_to_me"] is False
    await r.engine.aclose()


async def test_continuation_within_window_for_the_person_she_talked_to():
    r = await Rig(["Глэдос, привет", "а ещё вопрос", "я тоже хочу", "и ещё вопрос"], EngineConfig(owner_addressed=False)).open()
    await r.say("u2")                                              # позвал по имени: теперь она отвечает ему
    assert r.last()["data"]["addressed_reason"] == "name"
    r.engine.speak("s1", "Привет, Петя, как дела у тебя сегодня?")
    await r.clock.advance(3.0)
    await r.say("u2")
    assert r.last()["addressed_to_me"] is True and r.last()["data"]["addressed_reason"] == "continuation"
    await r.say("u3")                                              # другому человеку без имени продолжение не положено
    assert r.last()["addressed_to_me"] is False
    await r.clock.advance(60.0)                                    # окно 20 с кончилось
    await r.say("u2")
    assert r.last()["addressed_to_me"] is False
    await r.engine.aclose()


# ── контекст в тексте события ───────────────────────────────────────────────────

async def test_event_text_says_who_and_where():
    r = await Rig(["Расскажи анекдот.", "Глэдос, привет", "фоновая фраза"]).open(title="Aperture Science / Игры")
    await r.say("owner")
    assert r.last()["text"] == "Хозяин (владелец) в голосовом канале «Aperture Science / Игры»: «Расскажи анекдот.»"
    await r.say("u2")
    assert r.last()["text"].startswith("Петя в голосовом канале «Aperture Science / Игры»: «Глэдос, привет»")
    await r.engine.aclose()


async def test_event_text_without_title_and_with_dirty_nick():
    assert events.event_text(text="привет", name="Маша", is_owner=False, title="") == "Маша в голосовом канале: «привет»"
    nick = 'Ма"ша\n«кру\tтая»' + "я" * 80
    text = events.event_text(text="привет", name=nick, is_owner=True, title="Сервер\n«Канал»")
    who = text.split(" (владелец)")[0]
    assert "\n" not in text and '"' not in who and "«" not in who and len(who) <= events.NAME_MAX
    assert "в голосовом канале «Сервер 'Канал'»" in text and text.endswith("«привет»")
    assert events.event_text(text="т", name="  ", is_owner=False, title="").startswith("кто-то")


async def test_event_text_total_is_bounded():
    text = events.event_text(text="а" * 5000, name="н" * 500, is_owner=True, title="т" * 500, context="до этого в канале: " + "x" * 600)
    assert len(text) <= events.EVENT_MAX


async def test_tail_of_recent_phrases_in_order_with_her_replies():
    r = await Rig(["фоновая раз", "фоновая два", "Расскажи анекдот."]).open()
    await r.say("u2")
    await r.say("u3")
    r.engine.speak("s1", "Это моя реплика из канала.")
    await r.clock.advance(2.0)
    r.engine.ack("s1", r.sess.playback.current_uid or "", 1000, "completed")
    await r.clock.advance(5.0)
    await r.say("owner")
    ev = r.last()
    assert ev["data"]["addressed_reason"] == "owner"
    body = ev["text"]
    tail = body.split("\n", 1)[1]
    assert tail.startswith("(до этого в канале: Петя: фоновая раз; Вася: фоновая два; ты: Это моя реплика")
    assert "Расскажи анекдот." not in tail and len(body) <= 1000
    await r.engine.aclose()


async def test_tail_limits_count_age_and_ambient_has_none():
    r = await Rig([f"фраза номер {i}" for i in range(8)] + ["Расскажи анекдот.", "фоновая", "Расскажи ещё анекдот."]).open()
    for _ in range(8):
        await r.say("u2")
    assert all("до этого" not in e["text"] for e in r.events)           # у фоновых хвоста нет
    await r.say("owner")
    tail = r.last()["text"].split("\n", 1)[1]
    assert tail.count("Петя:") == 6 and "фраза номер 7" in tail and "фраза номер 1;" not in tail
    await r.clock.advance(120.0)                                         # всё старше минуты
    await r.say("u2")
    await r.say("owner")
    assert r.last()["text"].count("Петя:") == 1
    await r.engine.aclose()


async def test_transcript_tool_and_titles_in_sessions_tool():
    r = await Rig(["что-то про игру", "Расскажи анекдот."]).open(title="Игры")
    tools = {t.id: t for t in build_tools(r.engine)}
    assert "никто" in await tools["voice.transcript"].call({})
    await r.say("u2")
    await r.say("owner")
    r.engine.speak("s1", "Вот анекдот про робота.")
    await r.clock.advance(1.0)
    r.engine.ack("s1", r.sess.playback.current_uid, 800, "completed")
    await r.clock.advance(3.0)
    text = await tools["voice.transcript"].call({"n": 3})
    assert "Петя: что-то про игру" in text and "Хозяин: Расскажи анекдот." in text and "ты: Вот анекдот" in text
    assert text.index("Петя") < text.index("Хозяин") < text.index("ты:")
    assert len((await tools["voice.transcript"].call({"n": 1})).splitlines()) == 2
    assert "«Игры» (s1, fake)" in await tools["voice.sessions"].call({})
    assert r.engine.sessions()[0]["title"] == "Игры"
    await r.engine.aclose()


# ── задержка ────────────────────────────────────────────────────────────────────

async def test_latency_metrics_endpoint_stt_event_react():
    r = await Rig(["Расскажи анекдот."], stt_latency=0.3).open()
    r.engine.feed("s1", "owner", tone(600) + silence(800))
    await r.clock.advance(0.31)                                          # распознавание закончилось, событие подано
    assert len(r.events) == 1
    lat = r.sess.latency.public()
    assert lat["endpoint_ms"]["last_ms"] == 400 and lat["stt_ms"]["last_ms"] == 300
    assert lat["to_event_ms"]["last_ms"] == 700
    r.engine.speak("s1", "Вот анекдот про робота и лампочку.")
    await r.clock.advance(0.5)
    react = r.sess.latency.public()["react_ms"]
    assert react["count"] == 1 and 800 <= react["last_ms"] <= 1000 and react["avg_ms"] == react["last_ms"]
    r.engine.speak("s1", "Вторая реплика без нового вопроса.")                # без обращённой фразы react не пишется
    await r.clock.advance(6.0)
    assert r.sess.latency.public()["react_ms"]["count"] == 1
    snap = r.engine.snapshot()["latency"]
    assert snap["endpoint_ms"]["avg_ms"] == 400 and snap["react_ms"]["count"] == 1
    assert r.engine.sessions()[0]["latency"]["stt_ms"]["count"] == 1
    await r.engine.aclose()


def test_default_silence_tail_is_400ms():
    assert EngineConfig().tail_ms == 400 and EngineConfig.from_env({}).tail_ms == 400
    assert EngineConfig.from_env({"GITS_VOICE_TAIL_MS": "50"}).tail_ms == 200


# ── первое предложение ──────────────────────────────────────────────────────────

LONG_FIRST = "Ну смотри, это довольно длинное первое предложение, которое тянется и тянется без конца и края, пока не закончится."


def test_long_first_sentence_splits_at_first_comma_after_30_chars():
    out = split_sentences(LONG_FIRST + " Второе предложение уже обычное и спокойное.", first_max=90)
    assert out[0] == "Ну смотри, это довольно длинное первое предложение,"
    assert out[1].startswith("которое тянется") and out[-1] == "Второе предложение уже обычное и спокойное."
    assert split_sentences(LONG_FIRST, first_max=None) == [LONG_FIRST]                  # без настройки как раньше


def test_first_sentence_split_leaves_short_and_undelimited_alone():
    short = "Короткое первое предложение, совсем короткое."
    assert split_sentences(short, first_max=90) == [short]
    nodelim = "слово " * 25
    assert len(split_sentences(nodelim.strip() + ".", first_max=90)) == 1


async def test_first_sound_after_first_clause_of_long_reply():
    r = await Rig([]).open()
    r.engine.speak("s1", LONG_FIRST)
    await r.clock.advance(0.2)
    assert r.tts.calls[0] == "Ну смотри, это довольно длинное первое предложение,"
    assert r.transport.of("play.start")                                  # звук пошёл после короткого куска, а не всего предложения
    await r.engine.aclose()


def test_first_max_config():
    assert EngineConfig().tts_first_max == 90 and EngineConfig.from_env({"GITS_TTS_FIRST_MAX": "120"}).tts_first_max == 120


# ── ожидающие реплики ───────────────────────────────────────────────────────────

async def test_waiting_utterance_expires_after_ttl():
    r = await Rig([], EngineConfig(speech_ttl_s=3.0)).open()
    a = r.engine.speak("s1", "а" * 100)                                   # звучит около 4 с
    b = r.engine.speak("s1", "Устаревший ответ, который уже не нужен.")
    await r.clock.advance(10.0)
    ended = {e["utterance_id"]: e for e in r.speech if e["phase"] == "ended"}
    assert ended[a.uid]["completed"] and ended[b.uid]["reason"] == "expired" and ended[b.uid]["spoken_text"] == ""
    assert [s["utterance_id"] for s in r.transport.of("play.start")] == [a.uid]
    await r.engine.aclose()


async def test_fresh_waiting_utterance_still_plays():
    r = await Rig([], EngineConfig(speech_ttl_s=20.0)).open()
    r.engine.speak("s1", "а" * 100)
    b = r.engine.speak("s1", "Свежий ответ в очереди.")
    await r.clock.advance(12.0)
    assert [s["utterance_id"] for s in r.transport.of("play.start")][-1] == b.uid
    await r.engine.aclose()


async def test_new_addressed_phrase_drops_waiting_but_not_the_playing_one():
    r = await Rig(["Расскажи ещё."]).open()
    a = r.engine.speak("s1", "а" * 100)
    b = r.engine.speak("s1", "Ждущая реплика, которая устарела.")
    await r.clock.advance(1.0)
    await r.say("owner")
    ended = {e["utterance_id"]: e for e in r.speech if e["phase"] == "ended"}
    assert b.uid in ended and ended[b.uid]["reason"] == "replaced"
    assert a.uid not in ended and not r.transport.of("play.stop") and r.sess.playback.current_uid == a.uid
    await r.engine.aclose()


async def test_ambient_phrase_does_not_drop_waiting():
    r = await Rig(["болтают о своём"]).open()
    r.engine.speak("s1", "а" * 100)
    b = r.engine.speak("s1", "Ждущая реплика.")
    await r.clock.advance(1.0)
    await r.say("u2")
    assert b.uid in r.sess.playback.waiting_uids()
    await r.engine.aclose()


def test_config_new_settings_from_env():
    cfg = EngineConfig.from_env({"GITS_VOICE_OWNER_ADDRESSED": "0", "GITS_VOICE_CONVO_WINDOW_S": "5", "GITS_VOICE_AMBIENT_URGENCY": "9",
                                 "GITS_VOICE_SPEECH_TTL_S": "7"})
    assert cfg.owner_addressed is False and cfg.convo_window_s == 5 and cfg.ambient_urgency == 5 and cfg.speech_ttl_s == 7
    d = EngineConfig()
    assert d.owner_addressed and d.convo_window_s == 20 and d.ambient_urgency == 2 and d.speech_ttl_s == 20
