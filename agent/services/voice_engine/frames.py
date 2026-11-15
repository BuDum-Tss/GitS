"""Формат звука движка: кадры PCM s16le, 16000 Гц, моно, по 20 мс (320 отсчётов, 640 байт) и тип `AudioFrame`."""
from dataclasses import dataclass

import numpy as np

from gits_sdk.voice.protocol import (  # noqa: F401  (константы формата живут в протоколе: их делят движок и адаптеры)
    FRAME_BYTES,
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
)

BYTES_PER_MS = SAMPLE_RATE * SAMPLE_WIDTH // 1000      # 32
SILENCE_DB = -120.0


@dataclass(frozen=True)
class AudioFrame:
    """Один кадр звука говорящего: сессия, говорящий, номер, PCM и время получения (секунды эпохи)."""
    session: str
    speaker: str
    seq: int
    pcm: bytes
    ts: float


def pcm_ms(pcm: bytes) -> int:
    """Длительность PCM в миллисекундах."""
    return len(pcm) // BYTES_PER_MS


def split_frames(pcm: bytes) -> list[bytes]:
    """Нарезать PCM на кадры по 640 байт; последний неполный дополняется тишиной до целого кадра."""
    frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if frames and len(frames[-1]) < FRAME_BYTES:
        frames[-1] = frames[-1] + bytes(FRAME_BYTES - len(frames[-1]))
    return frames


def rms_db(pcm: bytes) -> float:
    """Уровень кадра в dBFS (0 это максимум, тишина около -120)."""
    if len(pcm) < SAMPLE_WIDTH:
        return SILENCE_DB
    samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % SAMPLE_WIDTH], dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(samples * samples))) / 32768.0
    return 20.0 * float(np.log10(rms)) if rms > 1e-6 else SILENCE_DB
