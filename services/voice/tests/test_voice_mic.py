"""Микрофон (перенесённые тесты ядра + VOICE-7, VOICE-8, VOICE-10): сегментатор, галлюцинации, ворота, user_speaking."""
import asyncio

import numpy as np

import mic_listener as ml
from stt_client import Transcript
from voice_rig import FakeSTT


def frame(speech: bool) -> bytes:
    return (np.full(ml.FRAME, 8000 if speech else 0, dtype=np.int16)).tobytes()


def test_segmenter_returns_utterance_after_silence():
    seg = ml.UtteranceSegmenter(lambda f: np.frombuffer(f, dtype=np.int16).max() > 100)
    out = None
    for _ in range(20):
        assert seg.feed(frame(True)) is None
    for _ in range(ml.SILENCE_FRAMES):
        out = seg.feed(frame(False)) or out
    assert out is not None and len(out[0]) > 20 * ml.FRAME_BYTES and out[1] is False


def test_segmenter_drops_clicks_and_marks_overlap():
    seg = ml.UtteranceSegmenter(lambda f: np.frombuffer(f, dtype=np.int16).max() > 100)
    for _ in range(3):
        seg.feed(frame(True))
    res = None
    for _ in range(ml.SILENCE_FRAMES):
        res = seg.feed(frame(False)) or res
    assert res is None                                   # слишком короткий звук: не речь
    for _ in range(10):
        seg.feed(frame(True), agent_speaking=True)
    for _ in range(ml.SILENCE_FRAMES):
        res = seg.feed(frame(False)) or res
    assert res is not None and res[1] is True            # перекрылась с её речью


def test_segmenter_reports_activity():
    seg = ml.UtteranceSegmenter(lambda f: f == b"s", silence_frames=2, min_speech=1)
    assert not seg.active
    seg.feed(b"s")
    assert seg.active
    seg.feed(b"-")
    assert seg.active
    assert seg.feed(b"-") is not None and not seg.active


def test_hallucinations():
    assert ml.is_hallucination("Продолжение следует...")
    assert ml.is_hallucination("Субтитры сделал DimaTorzok")
    assert not ml.is_hallucination("поставь таймер на пять минут")


def test_listener_passes_every_voice_as_the_owner_and_drops_junk():
    got = []

    async def on_voice(text, info, trace=None):
        got.append((text, info))

    class FakeSTT2:
        def __init__(self, text):
            self.t = Transcript(text=text)

        async def transcribe(self, *a, **k):
            return self.t

    async def run(text):
        lis = ml.MicListener(FakeSTT2(text), on_voice, lambda: False)
        await lis.handle_utterance(b"\0" * 100)

    asyncio.run(run("включи свет"))
    asyncio.run(run("Продолжение следует..."))
    asyncio.run(run("кто здесь"))
    assert [t for t, _ in got] == ["включи свет", "кто здесь"]
    assert all(info == {"id": "owner", "name": None, "owner": True} for _, info in got)


# ── ворота (VOICE-7) ─────────────────────────────────────────────────────────

def test_gate_follows_playback_and_echo_tail(tmp_path):
    t = [100.0]
    state = {"speaking": True}
    lis = ml.MicListener(None, None, lambda: state["speaking"], echo_tail=lambda: 0.3, clock=lambda: t[0])
    assert lis._gate() is True                                # она говорит: закрыто
    state["speaking"] = False
    t[0] += 0.2
    assert lis._gate() is True                                # хвост эха
    t[0] += 0.2
    assert lis._gate() is False                               # хвост прошёл
    lis2 = ml.MicListener(None, None, lambda: True, echo_tail=lambda: 0.0, clock=lambda: t[0])
    lis2._gate()
    assert lis2._tail_until == t[0]


async def test_overlapping_utterance_is_dropped_but_later_one_goes_through(tmp_path):
    stt = FakeSTT("привет")
    got = []

    async def on_voice(text, info, trace=None):
        got.append(text)
    state = {"speaking": True}
    lis = ml.MicListener(stt, on_voice, lambda: state["speaking"], echo_tail=lambda: 0.0, vad=lambda f: f[0] == 1)
    lis.prepare()
    for _ in range(15):
        await lis.handle_frame(b"\x01" * 960)
    for _ in range(ml.SILENCE_FRAMES):
        await lis.handle_frame(b"\x00" * 960)
    await asyncio.sleep(0.05)
    assert got == []                                          # её речь пересеклась с репликой: отброшена
    state["speaking"] = False
    for _ in range(15):
        await lis.handle_frame(b"\x01" * 960)
    for _ in range(ml.SILENCE_FRAMES):
        await lis.handle_frame(b"\x00" * 960)
    await asyncio.sleep(0.05)
    assert got == ["привет"]


# ── user_speaking (VOICE-8) ──────────────────────────────────────────────────

def test_user_speaking_is_sent_on_change_and_at_most_five_per_second():
    seen, t = [], [10.0]
    mic = ml.MicListener(None, None, lambda: False, on_speaking=seen.append, clock=lambda: t[0])
    mic._note_speaking(False)
    mic._note_speaking(True)
    assert seen == [True]
    t[0] += 0.05
    mic._note_speaking(False)                                 # слишком рано: откладывается
    assert seen == [True]
    mic.flush_speaking()
    assert seen == [True]
    t[0] += 0.2
    mic.flush_speaking()
    assert seen == [True, False]
    for i in range(20):                                       # дребезг: не больше одного сообщения за 0,2 с
        t[0] += 0.01
        mic._note_speaking(i % 2 == 0)
    assert len(seen) <= 3
    mic._note_speaking(False, force=True)                     # конец сессии: сообщить сразу
    assert seen[-1] is False


def test_user_speaking_survives_callback_errors_and_missing_listener():
    broken = ml.MicListener(None, None, lambda: False, on_speaking=lambda v: 1 / 0)
    broken._note_speaking(True)
    ml.MicListener(None, None, lambda: False)._note_speaking(True)


async def test_gated_frames_do_not_count_as_user_speaking():
    seen = []
    mic = ml.MicListener(None, None, lambda: True, on_speaking=seen.append, vad=lambda f: f[0] == 1)
    mic.prepare()
    for _ in range(10):
        await mic.handle_frame(b"\x01" * 960)                 # это её собственный голос из колонок
    assert seen == []
    quiet = ml.MicListener(None, None, lambda: False, on_speaking=seen.append, vad=lambda f: f[0] == 1)
    quiet.prepare()
    await quiet.handle_frame(b"\x01" * 960)
    assert seen == [True]


# ── детектор перебивания (VOICE-10) ──────────────────────────────────────────

def test_barge_detector_needs_800_ms_of_continuous_speech():
    d = ml.BargeInDetector()
    assert ml.BARGE_MIN_FRAMES * 30 >= 800
    for i in range(ml.BARGE_MIN_FRAMES - 1):
        assert d.feed(b"s", True) is False
    assert d.feed(b"s", True) is True
    assert len(d.audio()) == ml.BARGE_MIN_FRAMES


def test_barge_detector_tolerates_short_pauses_but_not_long_ones():
    d = ml.BargeInDetector(min_frames=10, max_gap=2)
    for _ in range(5):
        d.feed(b"s", True)
    d.feed(b"-", False)
    d.feed(b"-", False)
    for _ in range(4):
        d.feed(b"s", True)
    assert d.feed(b"s", True) is True                         # 10 кадров речи с короткой паузой внутри
    d2 = ml.BargeInDetector(min_frames=10, max_gap=2)
    for _ in range(9):
        d2.feed(b"s", True)
    for _ in range(3):
        d2.feed(b"-", False)                                  # пауза длиннее: фраза началась заново
    assert d2.feed(b"s", True) is False and d2.speech == 1


def test_barge_detector_silence_alone_never_triggers():
    d = ml.BargeInDetector()
    for _ in range(200):
        assert d.feed(b"-", False) is False
    assert d.frames == []
