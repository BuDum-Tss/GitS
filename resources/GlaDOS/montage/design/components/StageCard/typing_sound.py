"""Звук печати клавиатуры для карточек этапа и набора текста: дорожка щелчков по моментам символов (мс от начала вставки).
Использование: times, spaces = typing_times(['Этап', 'Кровать', 'как концепция'], start_ms=350, cps=16, gap_after_first=650);
src = make_typing('stage1', times, spaces)  -> public/typing/stage1.wav; путь передаётся в props.typing_src."""
from pathlib import Path

WORK = Path('work')   # корень проекта Remotion (папка work/ ролика)

import random
import wave
import struct


def make_typing(name, times_ms, spaces=()):
    """Дорожка щелчков клавиатуры по моментам печати символов (мс от начала вставки). Возвращает путь относительно public/."""
    sr = 44100
    out = WORK / "public" / "typing" / f"{name}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    total = int(sr * ((max(times_ms) if times_ms else 0) / 1000 + 0.4))
    buf = [0.0] * total
    rnd = random.Random(name)
    for k, tm in enumerate(times_ms):
        base = int(sr * (tm + rnd.uniform(-7, 7)) / 1000)
        if base < 0:
            continue
        amp = (0.22 if k in spaces else 0.5) * rnd.uniform(0.7, 1.0)
        tone = rnd.uniform(1800, 3200)
        for i in range(int(sr * 0.012)):
            if base + i >= total:
                break
            env = (1 - i / (sr * 0.012)) ** 3
            noise = rnd.uniform(-1, 1)
            buf[base + i] += amp * env * (0.65 * noise + 0.35 * __import__("math").sin(2 * 3.14159 * tone * i / sr))
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 30000)) for v in buf))
    return f"typing/{name}.wav"


def typing_times(text_list, start_ms, cps, gap_after_first=0):
    """Моменты печати каждого символа для строк, набираемых одна за другой."""
    times, spaces, at = [], set(), start_ms
    for li, text in enumerate(text_list):
        for ch in text:
            if ch == " ":
                spaces.add(len(times))
            times.append(at)
            at += 1000 / cps
        if li == 0:
            at += gap_after_first
    return times, spaces


