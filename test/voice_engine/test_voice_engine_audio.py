"""Перевод звука 48 кГц стерео <-> 16 кГц моно без audioop."""
import numpy as np

from gits_sdk.voice import audio


def sine(rate, ms, freq=1000.0, channels=1, amp=0.5):
    t = np.arange(rate * ms // 1000) / rate
    mono = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype("<i2")
    return (np.repeat(mono, 2) if channels == 2 else mono).tobytes()


def dominant_hz(pcm, rate):
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.argmax(spec) * rate / len(x)


def test_to_16k_mono_length_and_tone():
    out = audio.to_16k_mono(sine(48000, 1000, channels=2))
    assert len(out) == 16000 * 2
    assert abs(dominant_hz(out, 16000) - 1000) < 5
    rms = np.sqrt(np.mean(np.frombuffer(out, dtype="<i2").astype(np.float32) ** 2))
    assert 0.8 * 0.5 * 32767 / 1.414 < rms < 1.1 * 0.5 * 32767 / 1.414


def test_to_48k_stereo_length_and_channels():
    out = audio.to_48k_stereo(sine(16000, 500, 440))
    assert len(out) == 48000 // 2 * 2 * 2
    arr = np.frombuffer(out, dtype="<i2").reshape(-1, 2)
    assert (arr[:, 0] == arr[:, 1]).all()
    assert abs(dominant_hz(arr[:, 0].tobytes(), 48000) - 440) < 5


def test_resample_24k_to_16k():
    out = audio.resample(sine(24000, 1000, 700), 24000, 16000)
    assert len(out) == 16000 * 2
    assert abs(dominant_hz(out, 16000) - 700) < 5


def test_stream_chunks_match_one_shot():
    src = sine(48000, 400, 600, channels=2)
    rs = audio.to_16k_mono_stream()
    step = 960 * 4                       # 20 мс стерео 48 кГц
    streamed = b"".join(rs.process(src[i:i + step]) for i in range(0, len(src), step))
    whole = audio.Resampler(48000, 16000, 2, 1).process(src)
    assert streamed == whole


def test_stream_handles_odd_chunk_sizes():
    src = sine(48000, 200, 500, channels=2)
    rs = audio.to_16k_mono_stream()
    parts = [rs.process(src[i:i + 1001]) for i in range(0, len(src), 1001)]
    assert b"".join(parts) == audio.Resampler(48000, 16000, 2, 1).process(src)


def test_clipping_not_wrapping():
    loud = np.full(4800, 32767, dtype="<i2").tobytes()
    out = np.frombuffer(audio.resample(loud, 16000, 48000), dtype="<i2")
    assert out.max() <= 32767 and out.min() > -32768 * 0.5
