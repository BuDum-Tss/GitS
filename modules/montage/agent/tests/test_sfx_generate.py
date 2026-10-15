"""Синтез звуков (MON-23, MON-37): длительность, формат, пик, частоты, детерминизм, совпадение с файлами в репозитории."""
import wave

import generate                       # assets/sfx/generate.py (pythonpath в pyproject.toml)
import numpy as np
import pytest

NAMES = ["chime_up", "chime_down", "beep", "tick", "whoosh", "buzz_error", "elevator_ding"]


def test_all_required_sounds_are_defined():
    assert sorted(generate.SOUNDS) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_duration_peak_and_tail(name):
    fn, expected = generate.SOUNDS[name]
    x = fn()
    assert len(x) / generate.SAMPLE_RATE == pytest.approx(expected, abs=0.002)
    peak = float(np.max(np.abs(x)))
    assert 0.69 < peak <= generate.PEAK + 1e-9                        # около -3 dBFS, без клиппинга
    assert abs(float(x[-1])) < 0.01                                    # спад в конце: нет щелчка
    assert abs(float(np.mean(x))) < 0.01                               # без постоянной составляющей
    assert np.isfinite(x).all()


@pytest.mark.parametrize("name", NAMES)
def test_deterministic(name):
    fn, _ = generate.SOUNDS[name]
    assert np.array_equal(fn(), fn())


def _spectrum_peaks(x: np.ndarray, n: int = 2):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / generate.SAMPLE_RATE)
    idx = np.argsort(spec)[::-1]
    peaks: list[float] = []
    for i in idx:
        f = float(freqs[i])
        if all(abs(f - p) > 40 for p in peaks):
            peaks.append(f)
        if len(peaks) == n:
            break
    return peaks


def test_beep_is_1000_hz():
    assert _spectrum_peaks(generate.beep(), 1)[0] == pytest.approx(1000, abs=15)


def test_chime_up_has_both_notes():
    peaks = _spectrum_peaks(generate.chime_up(), 2)
    assert any(abs(p - 659.25) < 20 for p in peaks) and any(abs(p - 987.77) < 20 for p in peaks)


def test_elevator_ding_is_e6():
    assert _spectrum_peaks(generate.elevator_ding(), 1)[0] == pytest.approx(1318.5, abs=15)


def test_buzz_is_low_and_tick_is_bright():
    assert _spectrum_peaks(generate.buzz_error(), 1)[0] < 600
    spec = np.abs(np.fft.rfft(generate.tick()))
    freqs = np.fft.rfftfreq(len(generate.tick()), 1 / generate.SAMPLE_RATE)
    assert float(np.sum(spec[freqs > 1500])) > float(np.sum(spec[freqs < 500]))


def test_whoosh_is_noise_with_a_swell():
    x = generate.whoosh()
    n = len(x)
    middle = float(np.sqrt(np.mean(x[n // 3: 2 * n // 3] ** 2)))
    edges = float(np.sqrt(np.mean(np.concatenate([x[: n // 10], x[-n // 10:]]) ** 2)))
    assert middle > 4 * edges


def test_files_are_pcm16_mono_and_match_the_generator(tmp_path, root):
    generate.generate_all(tmp_path)
    for name in NAMES:
        fresh = tmp_path / f"{name}.wav"
        committed = root / "assets" / "sfx" / f"{name}.wav"
        assert committed.is_file(), f"{name}.wav не сгенерирован: python assets/sfx/generate.py"
        with wave.open(str(committed), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 44100)
            assert w.getnframes() / 44100 == pytest.approx(generate.SOUNDS[name][1], abs=0.002)
        assert committed.read_bytes() == fresh.read_bytes(), f"{name}.wav устарел: перегенерируй generate.py"


def test_template_copies_match(root):
    pub = root / "template" / "public" / "sfx"
    for name in NAMES:
        assert (pub / f"{name}.wav").read_bytes() == (root / "assets" / "sfx" / f"{name}.wav").read_bytes()
