"""Приведение звука к формату протокола (PCM s16le, 16 кГц, моно) и обратно: Discord даёт 48 кГц стерео, колонки хотят то же.

Без `audioop` (удалён из Python 3.13) и без scipy: только numpy. Ресемплер рациональный (L/M): вставка нулей, ФНЧ (окно
Блэкмана), прореживание; состояние (хвост фильтра и фаза прореживания) переносится между кусками, поэтому непрерывный поток
из кусков по 20 мс звучит без щелчков на границах. Одноразовые функции `to_16k_mono`, `to_48k_stereo` и `resample` дают
тот же результат целиком.
"""
from __future__ import annotations

from math import gcd

import numpy as np

SAMPLE_WIDTH = 2
_TAPS_PER_PHASE = 24                     # длина фильтра на единицу большего из L и M: компромисс срез/задержка


def _design_filter(up: int, down: int) -> np.ndarray:
    """ФНЧ для скорости (src * up): срез на 0.9 от меньшей из частот Найквиста, усиление up (компенсирует вставку нулей)."""
    taps = _TAPS_PER_PHASE * max(up, down) * 2 + 1
    cutoff = 0.9 * 0.5 / max(up, down)             # доля частоты дискретизации после вставки нулей
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(taps)
    return (h / h.sum() * up).astype(np.float32)


class Resampler:
    """Потоковый ресемплер PCM s16le: `process(pcm)` принимает куски любой длины, возвращает готовые куски.

    src_rate -> dst_rate (целые, отношение любое рациональное), каналы приводятся к dst_channels (1 или 2): стерео
    сводится в моно средним, моно разворачивается в стерео копией."""

    def __init__(self, src_rate: int, dst_rate: int, src_channels: int = 1, dst_channels: int = 1):
        if src_rate <= 0 or dst_rate <= 0 or src_channels not in (1, 2) or dst_channels not in (1, 2):
            raise ValueError("частоты должны быть положительными, каналов 1 или 2")
        g = gcd(src_rate, dst_rate)
        self.up, self.down = dst_rate // g, src_rate // g
        self.src_channels, self.dst_channels = src_channels, dst_channels
        self.src_rate, self.dst_rate = src_rate, dst_rate
        self._h = _design_filter(self.up, self.down) if (self.up, self.down) != (1, 1) else None
        self._hist = np.zeros(0 if self._h is None else len(self._h) - 1, dtype=np.float32)
        self._offset = 0                               # индекс первой выбираемой точки в следующем куске после вставки нулей
        self._rest = b""                               # недобранный хвост входа (неполный отсчёт)
        self._delay = 0 if self._h is None else (len(self._h) - 1) // 2    # групповая задержка фильтра, отсчётов после вставки

    def process(self, pcm: bytes) -> bytes:
        data = self._rest + pcm
        frame = SAMPLE_WIDTH * self.src_channels
        usable = len(data) - len(data) % frame
        self._rest = data[usable:]
        if not usable:
            return b""
        samples = np.frombuffer(data[:usable], dtype="<i2").astype(np.float32)
        mono = samples.reshape(-1, self.src_channels).mean(axis=1) if self.src_channels == 2 else samples
        out = self._resample(mono)
        return self._to_pcm(out)

    def _resample(self, mono: np.ndarray) -> np.ndarray:
        if self._h is None:
            return mono
        n = len(mono)
        stuffed = np.zeros(n * self.up, dtype=np.float32)
        stuffed[::self.up] = mono
        buf = np.concatenate((self._hist, stuffed))
        filtered = np.convolve(buf, self._h, mode="valid")           # ровно n * up точек
        self._hist = buf[len(buf) - (len(self._h) - 1):] if len(self._h) > 1 else buf[:0]
        picked = filtered[self._offset::self.down]
        self._offset = (self._offset - n * self.up) % self.down
        return picked

    def _to_pcm(self, mono: np.ndarray) -> bytes:
        clipped = np.clip(np.rint(mono), -32768, 32767).astype("<i2")
        if self.dst_channels == 2:
            clipped = np.repeat(clipped, 2)
        return clipped.tobytes()

    def flush(self) -> bytes:
        """Вытолкнуть хвост фильтра нулями (в конце потока); задержка фильтра при этом не срезается."""
        if self._h is None:
            return b""
        pad = np.zeros(self._delay // self.up + 1, dtype=np.float32)
        out = self._resample(pad)
        return self._to_pcm(out)


def resample(pcm: bytes, src_rate: int, dst_rate: int, src_channels: int = 1, dst_channels: int = 1) -> bytes:
    """Одноразовое преобразование целого куска: длина результата точно по отношению частот, фильтр без сдвига по времени."""
    if src_rate == dst_rate and src_channels == dst_channels:
        return pcm
    rs = Resampler(src_rate, dst_rate, src_channels, dst_channels)
    if rs._h is None:
        return rs.process(pcm)
    in_samples = len(pcm) // (SAMPLE_WIDTH * src_channels)
    want = in_samples * rs.up // rs.down
    # задержку фильтра срезаем с начала, нехватку в конце добираем выходом flush
    skip = rs._delay // rs.down + (1 if rs._delay % rs.down else 0)
    out = rs.process(pcm) + rs.flush()
    start = skip * SAMPLE_WIDTH * rs.dst_channels
    body = out[start:start + want * SAMPLE_WIDTH * rs.dst_channels]
    return body


def to_16k_mono(pcm_48k_stereo: bytes) -> bytes:
    """48 кГц стерео s16le (Discord, звуковая карта) -> 16 кГц моно s16le (формат протокола)."""
    return resample(pcm_48k_stereo, 48000, 16000, 2, 1)


def to_48k_stereo(pcm_16k: bytes) -> bytes:
    """16 кГц моно s16le (формат протокола) -> 48 кГц стерео s16le."""
    return resample(pcm_16k, 16000, 48000, 1, 2)


def to_16k_mono_stream() -> Resampler:
    """Потоковое 48 кГц стерео -> 16 кГц моно: звать `.process(кусок)` на каждый кусок потока одного говорящего."""
    return Resampler(48000, 16000, 2, 1)


def to_48k_stereo_stream() -> Resampler:
    """Потоковое 16 кГц моно -> 48 кГц стерео: для кадров воспроизведения одной реплики."""
    return Resampler(16000, 48000, 1, 2)
