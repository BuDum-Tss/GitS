"""Фильтр эха её собственной речи: эхо во время речи и после неё, окно времени, короткие фразы, выключение, перебивание."""
import pytest

from gits_sdk.voice.testing import silence, tone
from services.voice_engine import echo
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt
from services.voice_engine.testing import ClockTts, FakeClock, RecordingTransport
from services.voice_engine.vad import EnergyVad, SegmenterConfig, UtteranceSegmenter

HER = "Ага, это именно проверка нулевого заказа. Петь не буду, потому что это не входит в мои задачи."
ECHO = "Ага, это именно проверка нулевого."


class Rig:
    def __init__(self, texts, cfg=None):
        self.clock = FakeClock()
        self.events: list[dict] = []
        self.stt = FakeStt(texts)

        async def emit(e):
            self.events.append(e)

        self.engine = VoiceEngine(stt=self.stt, tts=ClockTts(self.clock, latency_s=0.1), config=cfg or EngineConfig(),
                                  clock=self.clock, emit_event=emit, names=lambda: ("Глэдос",))
        self.transport = RecordingTransport(self.clock)

    async def open(self):
        self.sess = await self.engine.open_session("s1", "fake", self.transport, Capabilities(echo_free=True, can_barge_in=True))
        self.engine.add_speaker("s1", "u1", "Маша", 1, is_owner=True)

    async def say(self, ms=600, amp=0.3):
        self.engine.feed("s1", "u1", tone(ms, amplitude=amp) + silence(800))
        await self.engine.settle()

    async def speak(self):
        self.engine.speak("s1", HER)
        await self.clock.advance(0.5)


@pytest.fixture
def make():
    rigs = []

    async def factory(texts, cfg=None):
        r = Rig(texts, cfg)
        await r.open()
        rigs.append(r)
        return r
    yield factory


async def test_echo_during_her_speech_is_dropped(make, caplog):
    r = await make([ECHO])
    await r.speak()
    caplog.set_level("INFO")
    await r.say()
    assert r.events == [] and r.sess.stats.echo_dropped == 1 and r.sess.stats.utterances == 0
    assert "эхо её речи отброшено" in caplog.text
    await r.engine.aclose()


async def test_echo_after_her_speech_within_window_dropped_then_passes_after_window(make):
    r = await make([ECHO, ECHO])
    await r.speak()
    await r.clock.advance(9.0)                            # реплика закончилась (около 4 с), прошло несколько секунд
    await r.say()
    assert r.events == [] and r.sess.stats.echo_dropped == 1
    await r.clock.advance(30.0)                           # окно 8 с давно закрыто: то же самое теперь слова человека
    await r.say()
    assert len(r.events) == 1 and r.sess.stats.echo_dropped == 1
    await r.engine.aclose()


async def test_different_words_pass_even_during_her_speech(make):
    r = await make(["Стой, подожди, я хочу задать другой вопрос"])
    await r.speak()
    await r.say()
    assert len(r.events) == 1 and r.events[0]["text"].startswith("Стой") and r.sess.stats.echo_dropped == 0
    await r.engine.aclose()


async def test_short_yes_passes_when_she_said_something_else(make):
    r = await make(["Да.", "заказа"])
    await r.speak()
    await r.say()
    assert [e["text"] for e in r.events] == ["Да."]
    await r.say()                                          # а целиком и подряд встречающееся в её речи слово это эхо
    assert len(r.events) == 1 and r.sess.stats.echo_dropped == 1
    await r.engine.aclose()


async def test_filter_off_with_zero_window(make):
    r = await make([ECHO], EngineConfig(echo_window_s=0))
    await r.speak()
    await r.say()
    assert len(r.events) == 1 and r.sess.stats.echo_dropped == 0
    await r.engine.aclose()


async def test_nothing_spoken_nothing_filtered(make):
    r = await make([ECHO])
    await r.say()
    assert len(r.events) == 1
    await r.engine.aclose()


async def test_barge_in_by_real_loud_speech_still_works(make):
    r = await make(["стоп"], EngineConfig(barge_in_ms=400))
    await r.speak()
    for _ in range(25):
        r.engine.feed("s1", "u1", tone(20, amplitude=0.3))
    await r.clock.settle()
    assert r.transport.of("play.stop") and r.transport.of("play.stop")[0]["reason"] == "barge_in"
    await r.engine.aclose()


async def test_quiet_echo_level_speech_does_not_barge_in(make):
    r = await make([ECHO], EngineConfig(barge_in_ms=400))
    await r.speak()
    for _ in range(100):                                   # 2 с речи на уровне эха: выше порога VAD, ниже порога перебивания
        r.engine.feed("s1", "u1", tone(20, amplitude=0.015))
    await r.clock.settle()
    assert r.transport.of("play.stop") == [] and r.sess.stats.barge_ins == 0
    await r.engine.aclose()


def test_loud_run_counts_only_loud_frames():
    seg = UtteranceSegmenter(EnergyVad(-42.0), SegmenterConfig(), loud_db=-36.0)
    seg.push(tone(300, amplitude=0.015))
    assert seg.run_ms == 300 and seg.loud_run_ms == 0
    seg.push(tone(200, amplitude=0.3))
    assert seg.loud_run_ms == 200
    seg.push(silence(200))
    assert seg.loud_run_ms == 0


def test_similarity_rules():
    assert echo.similar("Ага, это именно проверка нулевого.", HER, 0.7)
    assert echo.similar("это именно проверка нулевых заказов", HER, 0.7)           # другое окончание слов
    assert not echo.similar("а теперь расскажи про погоду завтра", HER, 0.7)
    assert not echo.similar("Да", HER, 0.7) and echo.similar("Петь не буду", HER, 0.7) and echo.similar("петь", HER, 0.7)
    assert not echo.similar("", HER, 0.7) and not echo.similar("слово", "", 0.7)


def test_config_echo_from_env():
    cfg = EngineConfig.from_env({"GITS_VOICE_ECHO_WINDOW_S": "0", "GITS_VOICE_ECHO_RATIO": "0.9", "GITS_VOICE_BARGE_IN_DB": "9"})
    assert cfg.echo_window_s == 0 and cfg.echo_ratio == 0.9 and cfg.barge_in_db == 9
    d = EngineConfig.from_env({})
    assert d.echo_window_s == 8.0 and d.echo_ratio == 0.7 and d.barge_in_db == 6.0
