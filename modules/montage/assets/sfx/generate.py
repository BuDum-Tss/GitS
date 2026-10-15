"""Синтез звуковых эффектов для роликов (ТЗ 20 MON-23, ТЗ 21 §5): синусы с огибающими и отфильтрованный шум.

numpy + wave, никаких сэмплов из игр и платных библиотек. Результат детерминирован (шум с фиксированным seed):
повторный запуск даёт те же байты. Запуск: `python assets/sfx/generate.py [папка]` (по умолчанию рядом со скриптом).
Файлы: 44100 Гц, моно, 16 бит, пик около -3 dBFS (громкость в ролике ставит компонент Sfx, на 10-14 дБ тише голоса).
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
PEAK = 0.708                  # -3 dBFS
FADE_OUT_S = 0.006            # короткий спад в конце: без щелчка

HERE = Path(__file__).resolve().parent


def _t(duration: float) -> np.ndarray:
    return np.arange(int(round(duration * SAMPLE_RATE))) / SAMPLE_RATE


def _attack_decay(t: np.ndarray, attack: float, tau: float) -> np.ndarray:
    """Линейный набор за attack секунд и экспоненциальный спад с постоянной tau."""
    rise = np.clip(t / attack, 0.0, 1.0) if attack > 0 else np.ones_like(t)
    return rise * np.exp(-t / tau)


def _place(buf: np.ndarray, part: np.ndarray, start_s: float) -> None:
    """Сложить part в buf, начиная с start_s секунд (хвост за пределами буфера обрезается)."""
    i = int(round(start_s * SAMPLE_RATE))
    n = min(len(part), len(buf) - i)
    if n > 0:
        buf[i:i + n] += part[:n]


def _finish(x: np.ndarray) -> np.ndarray:
    """Нормализация пика до PEAK и спад в конце."""
    x = x - np.mean(x)
    peak = float(np.max(np.abs(x)))
    if peak > 0:
        x = x * (PEAK / peak)
    n = int(FADE_OUT_S * SAMPLE_RATE)
    x[-n:] *= np.linspace(1.0, 0.0, n)
    return x


def _tone(freq: float, duration: float, attack: float, tau: float, partials=((1.0, 1.0),)) -> np.ndarray:
    t = _t(duration)
    wave_ = sum(amp * np.sin(2 * np.pi * freq * ratio * t) for ratio, amp in partials)
    return wave_ * _attack_decay(t, attack, tau)


def chime_up() -> np.ndarray:
    """Два мягких колокольчика вверх (E5, затем B5): успех, появление карточки камеры. 0,9 с."""
    out = np.zeros(int(0.9 * SAMPLE_RATE))
    partials = ((1.0, 1.0), (2.0, 0.28), (3.0, 0.08))
    _place(out, _tone(659.25, 0.9, 0.004, 0.20, partials), 0.0)
    _place(out, _tone(987.77, 0.72, 0.004, 0.22, partials), 0.16)
    return _finish(out)


def chime_down() -> np.ndarray:
    """Два колокольчика вниз (B5, затем F#5, затем D5): мягкий минус. 0,9 с."""
    out = np.zeros(int(0.9 * SAMPLE_RATE))
    partials = ((1.0, 1.0), (2.0, 0.22), (3.0, 0.06))
    _place(out, _tone(987.77, 0.9, 0.004, 0.18, partials), 0.0)
    _place(out, _tone(739.99, 0.78, 0.004, 0.18, partials), 0.13)
    _place(out, _tone(587.33, 0.62, 0.004, 0.22, partials), 0.26)
    return _finish(out)


def beep() -> np.ndarray:
    """Короткий ровный писк 1000 Гц. 0,18 с."""
    t = _t(0.18)
    env = np.minimum(np.clip(t / 0.005, 0, 1), np.clip((t[-1] - t) / 0.012, 0, 1))
    return _finish(np.sin(2 * np.pi * 1000.0 * t) * env)


def tick() -> np.ndarray:
    """Сухой щелчок: затухающий шум плюс короткий тон 2400 Гц. 0,06 с."""
    rng = np.random.default_rng(7)
    t = _t(0.06)
    noise = np.diff(rng.standard_normal(len(t) + 1))          # разность: убирает низкие частоты
    x = noise * np.exp(-t / 0.004) * 0.6 + np.sin(2 * np.pi * 2400.0 * t) * np.exp(-t / 0.007)
    return _finish(x)


def whoosh() -> np.ndarray:
    """Пролёт: шум через фильтр низких частот, частота среза идёт вверх и обратно. 0,7 с."""
    rng = np.random.default_rng(11)
    t = _t(0.7)
    x = rng.standard_normal(len(t))
    progress = t / t[-1]
    cutoff = 250.0 + 5200.0 * np.sin(np.pi * progress) ** 2          # 250 Гц -> 5450 Гц -> 250 Гц
    alpha = 1.0 - np.exp(-2 * np.pi * cutoff / SAMPLE_RATE)
    y = np.empty_like(x)
    prev = 0.0
    for i in range(len(x)):
        prev += alpha[i] * (x[i] - prev)
        y[i] = prev
    env = np.sin(np.pi * progress) ** 2
    return _finish(y * env)


def buzz_error() -> np.ndarray:
    """Низкий гудок ошибки: две слегка расстроенные пилы 140 и 147 Гц с дрожанием 22 Гц. 0,45 с."""
    t = _t(0.45)

    def saw(f: float) -> np.ndarray:
        return 2.0 * ((f * t) % 1.0) - 1.0

    x = saw(140.0) + saw(147.0)
    kernel = np.ones(6) / 6.0                                        # лёгкое сглаживание граней пилы
    x = np.convolve(x, kernel, mode="same")
    tremolo = 0.65 + 0.35 * np.sign(np.sin(2 * np.pi * 22.0 * t))
    env = np.minimum(np.clip(t / 0.003, 0, 1), np.clip((t[-1] - t) / 0.03, 0, 1))
    return _finish(x * tremolo * env)


def elevator_ding() -> np.ndarray:
    """Один длинный звон колокольчика лифта: E6 с неточными верхними обертонами. 1,6 с."""
    t = _t(1.6)
    x = np.zeros_like(t)
    for ratio, amp, tau in ((1.0, 1.0, 0.65), (2.76, 0.32, 0.28), (5.4, 0.12, 0.11)):
        x += amp * np.sin(2 * np.pi * 1318.51 * ratio * t) * np.exp(-t / tau)
    x *= np.clip(t / 0.002, 0, 1)
    return _finish(x)


# имя файла -> (функция, ожидаемая длительность, с)
SOUNDS = {
    "chime_up": (chime_up, 0.9),
    "chime_down": (chime_down, 0.9),
    "beep": (beep, 0.18),
    "tick": (tick, 0.06),
    "whoosh": (whoosh, 0.7),
    "buzz_error": (buzz_error, 0.45),
    "elevator_ding": (elevator_ding, 1.6),
}


def to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()


def write_wav(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(to_pcm16(x))


def generate_all(out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, (fn, _) in SOUNDS.items():
        path = out_dir / f"{name}.wav"
        write_wav(path, fn())
        written[name] = path
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    for name, p in generate_all(target).items():
        print(f"{name}: {p}")
