"""Конвейерный синтез по предложениям на поддельных часах: первый звук после первого предложения, без зазоров, разрывы, ошибки."""
import pytest

from gits_sdk.voice.testing import tone
from services.voice_engine.config import EngineConfig
from services.voice_engine.engine import VoiceEngine
from services.voice_engine.session import Capabilities
from services.voice_engine.stt import FakeStt
from services.voice_engine.testing import ClockTts, FakeClock, RecordingTransport

def mk(ch: str) -> str:
    return ch.upper() + ch * 28 + "."                    # предложение из 30 символов (с заглавной: так оно начинается в речи)


S1, S2, S3 = mk("а"), mk("б"), mk("в")                   # 1200 мс звука каждое
TEXT = f"{S1} {S2} {S3}"
SENT_MS = 1200
FRAMES = SENT_MS // 20


class Rig:
    def __init__(self, tts_kw=None, cfg=None):
        self.clock = FakeClock()
        self.tts = ClockTts(self.clock, **(tts_kw or {}))
        self.speech: list[dict] = []
        self.journal: list[tuple] = []
        self.engine = VoiceEngine(stt=FakeStt(), tts=self.tts, config=cfg or EngineConfig(barge_in_ms=400), clock=self.clock,
                                  journal=lambda k, t, d: self.journal.append((k, t, d)), on_speech=self.speech.append)
        self.transport = RecordingTransport(self.clock)

    async def open(self):
        self.sess = await self.engine.open_session("s1", "fake", self.transport, Capabilities(echo_free=True, can_barge_in=True))
        return self.sess

    def ended(self):
        return [s for s in self.speech if s["phase"] == "ended"]

    def phases(self):
        return [s["phase"] for s in self.speech]

    def frame_times(self):
        return [f[0] for f in self.transport.frames]


@pytest.fixture
async def rig():
    r = Rig()
    await r.open()
    yield r
    await r.engine.aclose()


async def test_first_sound_after_first_sentence_only(rig):
    rig.engine.speak("s1", TEXT)
    await rig.clock.advance(0.49)
    assert rig.transport.frames == [] and not rig.transport.of("play.start")
    await rig.clock.advance(0.02)                                   # первое предложение синтезировано за 0.5 с
    assert rig.transport.of("play.start") and len(rig.transport.frames) >= 1
    assert rig.clock.monotonic() < 0.6 + 1000                       # всю реплику (3 x 0.5 с) не ждали
    m = rig.engine.snapshot()["metrics"]
    assert m["first_audio_last_ms"] == 500 and m["first_audio_avg_ms"] == 500 and m["utterances"] == 1


async def test_prefetch_gives_no_gap_between_sentences(rig):
    rig.engine.speak("s1", TEXT)
    await rig.clock.advance(6.0)
    times = rig.frame_times()
    assert len(times) == 3 * FRAMES
    gaps = [round(times[i + 1] - times[i], 6) for i in range(3, len(times) - 1)]       # первые три кадра идут разом (lead)
    assert all(abs(g - 0.02) < 1e-6 for g in gaps), "между предложениями нет зазора"
    assert rig.engine.snapshot()["metrics"]["gaps"] == 0
    assert rig.tts.calls == [S1, S2, S3] and rig.tts.max_active == 1


async def test_events_started_segment_ended_and_spoken_upto_in_sentences(rig):
    ticket = rig.engine.speak("s1", TEXT)
    await rig.clock.advance(6.0)
    rig.engine.ack("s1", ticket.uid, 3 * SENT_MS, "completed")
    await rig.clock.settle()
    assert rig.phases() == ["started", "segment", "segment", "ended"]
    assert [s["seq"] for s in rig.speech if s["phase"] == "segment"] == [1, 2]
    assert [s["segment"] for s in rig.speech if s["phase"] == "segment"] == [S2, S3]
    end = rig.ended()[0]
    assert end["completed"] and end["spoken_upto"] == 3 and end["spoken_ms"] == 3 * SENT_MS and end["spoken_text"] == TEXT
    assert rig.journal[-1][1] == TEXT and rig.journal[-1][2]["spoken_segments"] == 3
    assert rig.transport.of("play.start")[0]["text"] == TEXT          # адаптеру весь текст, звук непрерывным потоком


async def test_slow_synthesis_makes_gap_not_silence():
    r = Rig({"latencies": [0.5, 3.0, 0.1]})
    await r.open()
    r.engine.speak("s1", TEXT)
    await r.clock.advance(7.0)
    times = r.frame_times()
    assert len(times) == 3 * FRAMES
    big = [(i, round(times[i + 1] - times[i], 3)) for i in range(len(times) - 1) if times[i + 1] - times[i] > 0.03]
    assert [i for i, _ in big] == [FRAMES - 1]                         # пауза ровно на стыке 1 и 2 предложения
    m = r.engine.snapshot()["metrics"]
    assert m["gaps"] == 1 and 1500 <= m["gap_last_ms"] <= 1900 and r.sess.playback.metrics.gaps == 1
    assert r.sess.public()["metrics"]["gaps"] == 1
    await r.engine.aclose()


async def test_synthesis_error_of_one_sentence_skips_it():
    r = Rig({"fail_on": ("б",)})
    await r.open()
    ticket = r.engine.speak("s1", TEXT)
    await r.clock.advance(6.0)
    r.engine.ack("s1", ticket.uid, 2 * SENT_MS, "completed")
    await r.clock.settle()
    end = r.ended()[0]
    assert end["completed"] and end["spoken_upto"] == 2 and end["spoken_text"] == f"{S1} {S3}"
    assert len(r.transport.frames) == 2 * FRAMES
    await r.engine.aclose()


async def test_all_sentences_failed_ends_with_error():
    r = Rig({"fail_on": ("а", "б", "в")})
    await r.open()
    r.engine.speak("s1", TEXT)
    await r.clock.advance(3.0)
    end = r.ended()[0]
    assert end["completed"] is False and end["reason"] == "error" and end["spoken_text"] == "" and not r.transport.of("play.start")
    assert not r.journal
    await r.engine.aclose()


async def test_prefetch_depth_and_split_off():
    r = Rig(cfg=EngineConfig(tts_prefetch=0))
    await r.open()
    r.engine.speak("s1", TEXT)
    await r.clock.advance(0.6)
    assert r.tts.calls == [S1]                                         # без предзагрузки второе не синтезируется заранее
    await r.engine.aclose()
    r = Rig(cfg=EngineConfig(tts_prefetch=2))
    await r.open()
    r.engine.speak("s1", TEXT)
    await r.clock.advance(1.6)
    assert r.tts.calls == [S1, S2, S3]
    await r.engine.aclose()
    r = Rig(cfg=EngineConfig(tts_split="off"))
    await r.open()
    r.engine.speak("s1", TEXT)
    await r.clock.advance(0.6)
    assert r.tts.calls == [TEXT]
    await r.engine.aclose()


async def test_tts_requests_one_at_a_time_across_queue(rig):
    rig.engine.speak("s1", TEXT)
    rig.engine.speak("s1", f"{mk('г')} {mk('г')[:-1]}ё.")
    rig.engine.speak("s1", f"{mk('д')} {mk('е')}")
    await rig.clock.advance(12.0)
    assert rig.tts.max_active == 1 and len(rig.tts.calls) >= 5


async def test_second_utterance_starts_without_new_synthesis_wait(rig):
    a = rig.engine.speak("s1", S1)
    b = rig.engine.speak("s1", S2)                                      # первое предложение ожидающей синтезируется заранее
    await rig.clock.advance(1.4)
    assert S2 in rig.tts.calls
    rig.engine.ack("s1", a.uid, SENT_MS, "completed")
    await rig.clock.advance(1.0)
    assert [s["utterance_id"] for s in rig.transport.of("play.start")] == [a.uid, b.uid]


async def test_stop_segment_end_finishes_current_sentence_only(rig):
    ticket = rig.engine.speak("s1", TEXT)
    await rig.clock.advance(1.0)                                        # звучит первое предложение
    rig.engine.stop("s1", "segment_end")
    await rig.clock.advance(3.0)
    assert len(rig.transport.frames) == FRAMES and not rig.transport.of("play.stop")
    assert rig.transport.of("play.end")
    rig.engine.ack("s1", ticket.uid, SENT_MS, "completed")
    await rig.clock.settle()
    end = rig.ended()[0]
    assert end["completed"] is False and end["reason"] == "stop" and end["spoken_upto"] == 1 and end["spoken_text"] == S1


async def test_stop_segment_end_on_last_sentence_is_completed(rig):
    ticket = rig.engine.speak("s1", S1)
    await rig.clock.advance(1.0)
    rig.engine.stop("s1", "segment_end")
    await rig.clock.advance(3.0)
    rig.engine.ack("s1", ticket.uid, SENT_MS, "completed")
    await rig.clock.settle()
    assert rig.ended()[0]["completed"] is True


async def test_stop_now_mid_second_sentence_reports_partial_text(rig):
    ticket = rig.engine.speak("s1", TEXT)
    await rig.clock.advance(0.5 + 1.2 + 0.6)                            # второе предложение прозвучало наполовину
    rig.engine.stop("s1", "now")
    await rig.clock.settle()
    assert rig.transport.of("play.stop")[0]["reason"] == "stop"
    rig.engine.ack("s1", ticket.uid, SENT_MS + 600, "stop")
    await rig.clock.settle()
    end = rig.ended()[0]
    assert end["spoken_upto"] == 1 and end["spoken_ms"] == SENT_MS + 600
    assert end["spoken_text"].startswith(S1) and len(end["spoken_text"]) > len(S1) and len(end["spoken_text"]) < len(S1) + 1 + len(S2)
    assert rig.journal[-1][1] == end["spoken_text"] and rig.journal[-1][2]["completed"] is False


async def test_replace_during_multisentence_reply(rig):
    a = rig.engine.speak("s1", TEXT)
    await rig.clock.advance(2.0)
    b = rig.engine.speak("s1", S3, replace=True)
    await rig.clock.settle()
    rig.engine.ack("s1", a.uid, 1500, "replaced")
    await rig.clock.advance(6.0)
    reasons = {e["utterance_id"]: e.get("reason") for e in rig.ended()}
    assert reasons[a.uid] == "replaced" and reasons[b.uid] is None
    assert rig.transport.of("play.start")[-1]["utterance_id"] == b.uid


async def test_barge_in_in_the_middle_of_a_sentence():
    r = Rig()
    await r.open()
    r.engine.add_speaker("s1", "u1", "Маша", 1)
    ticket = r.engine.speak("s1", TEXT)
    await r.clock.advance(0.5 + 1.2 + 0.3)
    for _ in range(25):
        r.engine.feed("s1", "u1", tone(20))
    await r.clock.settle()
    stop = r.transport.of("play.stop")
    assert len(stop) == 1 and stop[0]["reason"] == "barge_in"
    r.engine.ack("s1", ticket.uid, SENT_MS + 300, "barge_in")
    await r.clock.settle()
    end = r.ended()[0]
    assert end["reason"] == "barge_in" and end["spoken_upto"] == 1 and S1 in end["spoken_text"]
    assert S3 not in end["spoken_text"] and r.journal[-1][2]["interrupted"] is True
    await r.engine.aclose()


def test_config_split_and_prefetch_from_env():
    cfg = EngineConfig.from_env({"TTS_SPLIT": "off", "TTS_PREFETCH": "2"})
    assert cfg.tts_split == "off" and cfg.tts_prefetch == 2
    cfg = EngineConfig.from_env({"GITS_TTS_PREFETCH": "9"})
    assert cfg.tts_split == "sentences" and cfg.tts_prefetch == 3
    assert EngineConfig().tts_prefetch == 1 and EngineConfig().split_min_chars == 25 and EngineConfig().split_max_chars == 220


def test_barge_in_needs_two_seconds_of_speech_by_default_and_can_be_changed_or_turned_off():
    """Перебивать её надо речью дольше 2 секунд подряд (пользователь: короткие реплики и покашливание не должны её обрывать)."""
    assert EngineConfig().barge_in_ms == 2000
    assert EngineConfig.from_env({"GITS_STT_URL": "http://s", "GITS_TTS_URL": "http://t"}).barge_in_ms == 2000
    env = {"GITS_STT_URL": "http://s", "GITS_TTS_URL": "http://t"}
    assert EngineConfig.from_env({**env, "GITS_VOICE_BARGE_IN_MS": "1200"}).barge_in_ms == 1200
    assert EngineConfig.from_env({**env, "GITS_VOICE_BARGE_IN": "off"}).barge_in == "off"
