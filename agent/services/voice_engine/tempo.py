"""Ускорение речи без смены высоты голоса: растяжение по времени (WSOLA) для PCM 16 кГц моно.

Облачный синтез не умеет менять темп (параметр `speed` длину не меняет), поэтому «чуть быстрее» делаем сами. WSOLA: окна по 25 мс
с перекрытием 50% (окно Ханна), входной шаг `шаг_вывода * rate`; положение каждого окна уточняется поиском по нормированной
корреляции с естественным продолжением предыдущего, поэтому на стыках нет хруста, а высота тона не плывёт. Нормировка по сумме
окон: громкость сохраняется, края не проседают. Блокирующая работа (десятки миллисекунд на предложение): звать через
`asyncio.to_thread`.
"""
import numpy as np

SAMPLE_RATE = 16000
FRAME_MS = 25
SEARCH_MS = 8                  # на сколько вперёд и назад ищется лучший стык
MIN_RATE_DELTA = 0.01          # отличие темпа меньше этого не стоит искажений: вернуть как есть


def time_stretch(pcm: bytes, rate: float) -> bytes:
    """PCM s16le 16 кГц моно, ускоренный в `rate` раз (rate > 1 быстрее, длительность делится на rate), высота тона прежняя.
    Слишком короткий звук и темп, близкий к 1, возвращаются без изменений."""
    if abs(rate - 1.0) < MIN_RATE_DELTA or rate <= 0:
        return pcm
    x = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
    n = len(x)
    win_len = SAMPLE_RATE * FRAME_MS // 1000
    if n < 4 * win_len:
        return pcm
    hop_out = win_len // 2
    hop_in = hop_out * rate
    tol = SAMPLE_RATE * SEARCH_MS // 1000
    out_len = int(n / rate)
    frames = out_len // hop_out + 2
    window = np.hanning(win_len + 1)[:-1].astype(np.float32)            # периодический Ханн: при 50% перекрытии сумма равна 1
    src = np.concatenate((x, np.zeros(win_len + 2 * tol + hop_out, dtype=np.float32)))
    out = np.zeros(frames * hop_out + win_len, dtype=np.float32)
    norm = np.zeros_like(out)
    prev = 0
    for k in range(frames):
        nominal = int(round(k * hop_in))
        if k == 0:
            start = 0
        else:
            target = src[prev + hop_out:prev + hop_out + win_len]
            lo = max(0, nominal - tol)
            hi = min(n, nominal + tol)
            if hi <= lo:
                start = min(nominal, n)
            else:
                region = src[lo:hi + win_len]
                corr = np.correlate(region, target, mode="valid")        # len = hi - lo + 1
                energy = np.cumsum(np.concatenate(([0.0], region * region)))
                seg_energy = energy[win_len:win_len + len(corr)] - energy[:len(corr)]
                score = corr / np.sqrt(seg_energy + 1e-3)
                start = lo + int(np.argmax(score))
        seg = src[start:start + win_len]
        pos = k * hop_out
        out[pos:pos + win_len] += seg * window
        norm[pos:pos + win_len] += window
        prev = start
    out = out[:out_len]
    norm = norm[:out_len]
    out = np.where(norm > 1e-6, out / np.maximum(norm, 1e-6), 0.0)
    return np.clip(np.rint(out), -32768, 32767).astype("<i2").tobytes()
