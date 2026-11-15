"""Определение речи и нарезка на реплики."""
from gits_sdk.voice.testing import silence, tone
from services.voice_engine.frames import FRAME_BYTES, rms_db, split_frames
from services.voice_engine.vad import EnergyVad, SegmenterConfig, UtteranceSegmenter, load_silero_factory


def seg(**cfg):
    return UtteranceSegmenter(EnergyVad(-42.0), SegmenterConfig(**cfg))


def test_energy_vad_threshold():
    vad = EnergyVad(-40.0)
    assert vad.is_speech(tone(20, amplitude=0.3)[:FRAME_BYTES])
    assert not vad.is_speech(tone(20, amplitude=0.001)[:FRAME_BYTES])
    assert not vad.is_speech(silence(20))
    assert rms_db(silence(20)) < -100


def test_split_frames_pads_last():
    frames = split_frames(b"\x01\x00" * 400)
    assert len(frames) == 2 and all(len(f) == FRAME_BYTES for f in frames)
    assert frames[1][-2:] == b"\x00\x00"


def test_one_utterance_closed_by_tail():
    s = seg()
    assert s.push(silence(200) + tone(800)) == []
    assert s.speaking
    out = s.push(silence(700))
    assert len(out) == 1
    assert 800 <= out[0].speech_ms <= 900
    assert 1000 <= out[0].duration_ms <= 1300          # предзвук 200 мс, речь 800, хвост не длиннее 200
    assert not s.speaking


def test_click_discarded():
    s = seg()
    assert s.push(tone(100) + silence(900)) == []      # короче min_speech_ms 240
    assert not s.speaking


def test_pause_inside_speech_does_not_split():
    s = seg()
    out = s.push(tone(500) + silence(300) + tone(500) + silence(700))
    assert len(out) == 1 and out[0].speech_ms >= 1000


def test_two_utterances_in_a_row():
    s = seg()
    out = s.push(tone(500) + silence(700) + tone(500) + silence(700))
    assert len(out) == 2


def test_max_length_forces_close():
    s = seg(max_ms=1000)
    out = s.push(tone(2500))
    assert len(out) >= 2 and all(o.forced for o in out)


def test_flush_closes_started_utterance():
    s = seg()
    s.push(tone(600))
    tail = s.flush()
    assert tail is not None and tail.forced and tail.speech_ms >= 500
    assert s.flush() is None


def test_push_accepts_any_chunking():
    s1, s2 = seg(), seg()
    data = silence(100) + tone(700) + silence(700)
    a = s1.push(data)
    b = []
    for i in range(0, len(data), 333 * 2):
        b += s2.push(data[i:i + 333 * 2])
    assert [x.pcm for x in a] == [x.pcm for x in b]


def test_run_ms_tolerates_short_gaps_only():
    s = seg()
    s.push(tone(200))
    assert s.run_ms == 200
    s.push(silence(60) + tone(100))
    assert s.run_ms == 200 + 60 + 100
    s.push(silence(140))
    assert s.run_ms == 0


def test_silero_absent_returns_none(tmp_path):
    assert load_silero_factory("") is None
    assert load_silero_factory(str(tmp_path / "нет.onnx")) is None
