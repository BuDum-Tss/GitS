"""Перекодирование аудио в OGG/Opus через ffmpeg: нужно для голосовых сообщений Telegram.

sendVoice принимает только OGG-контейнер с кодеком Opus (иначе не будет «голосовым» с круглым плеером и волной).
Источник: файл любого формата, который умеет ffmpeg (WAV от Fish Speech, MP3 от edge-tts), или сырой PCM.
Перенесено из проекта GitS (src/audio/opus_encoder.py) и расширено вводом из файла.
Это копия agent/opus_encoder.py (ТЗ 10, TG-1): ядру он нужен для core.tts до ТЗ 12, модулю - для голосового ответа (deliver).
"""
import asyncio
import glob
import os
import shutil

# winget добавляет ffmpeg в PATH только для новых сессий: если процесс стартовал раньше, ищем по типовым путям
_FALLBACKS = (
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    "C:/Users/*/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe",
)
OPUS_SAMPLE_RATE = 48000


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for pattern in _FALLBACKS:
        for m in glob.glob(pattern):
            return m
    raise FileNotFoundError("ffmpeg не найден. Установи: winget install --id Gyan.FFmpeg")


async def _run(args: list[str], data: bytes | None = None) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        find_ffmpeg(), *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000,   # CREATE_NO_WINDOW: без мигающего окна консоли
    )
    out, err = await proc.communicate(input=data)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg завершился с ошибкой ({proc.returncode}): {err.decode(errors='replace')[-400:]}")
    return out


_OUT = ["-ar", str(OPUS_SAMPLE_RATE), "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"]


async def audio_file_to_ogg_opus(path: str) -> bytes:
    """Файл (wav, mp3 и т.п.) → байты OGG/Opus."""
    return await _run(["-v", "error", "-i", path, *_OUT])


async def audio_file_to_wav_file(src: str, dst: str) -> None:
    """Файл (mp3, ogg и т.п.) -> WAV-файл с нормальным заголовком (core.tts: длительность читается по заголовку)."""
    await _run(["-v", "error", "-y", "-i", src, dst])


async def audio_file_to_pcm16k(path: str) -> bytes:
    """Файл (wav, mp3) → сырой PCM int16 моно 16 кГц: нужен, чтобы отдать образец голоса на распознавание."""
    return await _run(["-v", "error", "-i", path, "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1"])


async def pcm_to_ogg_opus(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Сырой PCM int16 little-endian → байты OGG/Opus (ресемплинг до 48 кГц делает ffmpeg)."""
    return await _run(["-v", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "-i", "pipe:0", *_OUT],
                      pcm)
