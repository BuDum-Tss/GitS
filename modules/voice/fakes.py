"""Подмены для проверки запуска без звука (`GITS_VOICE_FAKE=1`, ТЗ 12): синтез пишет тишину, воспроизведение только ждёт.

Реальный синтез (Fish Speech, edge-tts), колонки, микрофон и слух системного звука в этом режиме не трогаются.
"""
import asyncio
import os
import tempfile
import wave
from pathlib import Path

FAKE_SEGMENT_S_ENV = "GITS_VOICE_FAKE_SEGMENT_MS"        # сколько «звучит» один сегмент, мс (по умолчанию 300)


def segment_seconds() -> float:
    try:
        return max(0, int(os.getenv(FAKE_SEGMENT_S_ENV, "300"))) / 1000.0
    except ValueError:
        return 0.3


def write_silence(directory: Path | None, ms: int = 200) -> str:
    """Тишина в WAV (16 кГц, моно): готовый «синтез»."""
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".wav", prefix="fake_", dir=str(directory) if directory is not None else None)
    os.close(fd)
    with wave.open(name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * (16 * ms))
    return name


def fake_synth_file(text: str, directory: Path | None = None) -> str | None:
    """Замена tts_engine.synth_file: тишина вместо голоса (пустой текст - ошибка, как у настоящего)."""
    return write_silence(directory) if text and text.strip() else None


class FakePlayer:
    """Ничего не играет, только ждёт: отмену (обрыв «сразу») принимает как настоящий."""

    def __init__(self, seconds: float | None = None):
        self.seconds = segment_seconds() if seconds is None else seconds
        self.played: list[str] = []

    async def play(self, path: str, volume: float) -> None:
        self.played.append(path)
        await asyncio.sleep(self.seconds)
