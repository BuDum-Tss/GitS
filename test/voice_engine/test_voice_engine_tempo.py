"""Растяжение по времени без смены высоты (tempo.py)."""
import time

import numpy as np
import pytest

from services.voice_engine.tempo import time_stretch

SR = 16000


def voiced(ms=2000, freq=440.0, amp=0.5, vibrato=True):
    t = np.arange(SR * ms // 1000) / SR
    env = (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) if vibrato else 1.0
    return (np.sin(2 * np.pi * freq * t) * amp * 32767 * env).astype("<i2")


def dominant_hz(samples):
    x = samples.astype(np.float32)
    return np.argmax(np.abs(np.fft.rfft(x * np.hanning(len(x))))) * SR / len(x)


def rms(samples):
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


@pytest.mark.parametrize("rate", [1.08, 1.25, 1.5, 0.85])
def test_duration_pitch_and_level(rate):
    src = voiced()
    out = np.frombuffer(time_stretch(src.tobytes(), rate), dtype="<i2")
    assert abs(len(out) / (len(src) / rate) - 1) < 0.03
    assert abs(dominant_hz(out) - 440) < 5                         # высота тона не меняется
    assert 0.95 < rms(out) / rms(src) < 1.05                       # громкость сохраняется


def test_two_tones_keep_both_frequencies():
    t = np.arange(SR * 2) / SR
    src = ((np.sin(2 * np.pi * 300 * t) + np.sin(2 * np.pi * 1200 * t)) * 0.25 * 32767).astype("<i2")
    out = np.frombuffer(time_stretch(src.tobytes(), 1.2), dtype="<i2").astype(np.float32)
    spec = np.abs(np.fft.rfft(out * np.hanning(len(out))))
    peaks = sorted(np.argsort(spec)[-2:] * SR / len(out))
    assert abs(peaks[0] - 300) < 8 and abs(peaks[1] - 1200) < 8


def test_no_clipping_on_loud_signal():
    src = voiced(amp=0.999, vibrato=False)
    out = np.frombuffer(time_stretch(src.tobytes(), 1.3), dtype="<i2")
    assert out.max() <= 32767 and out.min() >= -32768 and rms(out) / rms(src) < 1.1


def test_no_clicks_at_joins():
    src = voiced(vibrato=False, amp=0.4)
    out = np.frombuffer(time_stretch(src.tobytes(), 1.3), dtype="<i2").astype(np.float32)
    jump = np.abs(np.diff(out)).max()
    assert jump < 0.4 * 32767 * 0.3                                  # скачок между соседними отсчётами не больше обычного хода тона


def test_rate_near_one_and_bad_input_returned_as_is():
    pcm = voiced(500).tobytes()
    assert time_stretch(pcm, 1.0) == pcm and time_stretch(pcm, 1.005) == pcm
    assert time_stretch(b"", 1.2) == b"" and time_stretch(b"\x01", 1.2) == b"\x01"
    short = voiced(40).tobytes()
    assert time_stretch(short, 1.2) == short                         # слишком короткий звук не искажаем
    assert time_stretch(pcm, 0) == pcm


def test_silence_stays_silence_and_cost_is_small():
    out = time_stretch(bytes(SR * 2 * 2), 1.2)
    assert not any(out)
    long = voiced(6000).tobytes()                                    # около 220 символов речи
    started = time.perf_counter()
    time_stretch(long, 1.08)
    assert time.perf_counter() - started < 0.5
