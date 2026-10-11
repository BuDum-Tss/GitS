"""
Синтез речи ГЛаДОС (ТЗ 12, VOICE-1): текст в аудиофайл. Воспроизведение и очередь реплик это `speaker.py` и `player.py`.

Приоритет:
  1. Fish Speech S2-Pro (http://127.0.0.1:8080): клонирование голоса по GlaDos.wav
  2. edge-tts, запасной (ru-RU-SvetlanaNeural, pitch=-25Hz): если Fish Speech недоступен

Оба пути записывают WAV/MP3 во временный файл. Блокирующий вызов: гонять через `asyncio.to_thread`.
"""
import asyncio
import logging
import os
import re
import struct
import tempfile
import time
from pathlib import Path

import httpx

logger = logging.getLogger("tts")

# ── Fish Speech ────────────────────────────────────────────────────────────────

FISH_URL       = os.getenv("FISH_URL", "http://127.0.0.1:8080")
# Референс голоса лежит у модуля: modules/voice/resources/tts/references/GlaDos.wav (docker-compose монтирует ту же папку)
FISH_REFERENCE = Path(os.getenv("FISH_REFERENCE_AUDIO") or (Path(__file__).parent / "resources" / "tts" / "references" / "GlaDos.wav"))
FISH_TIMEOUT   = 30.0
FISH_SR        = 44100   # Fish Speech S2-Pro default
FISH_RECHECK_S = 60.0    # недоступный Fish Speech проверяется заново не чаще раза в минуту (раньше: только один раз за запуск)

# ── edge-tts fallback ─────────────────────────────────────────────────────────

VOICE    = "ru-RU-SvetlanaNeural"
PITCH    = "-25Hz"
RATE     = "-12%"

MAX_CHARS = 600

# Кешируем референс при первом использовании
_reference_bytes: bytes | None = None
_fish_available: bool | None   = None   # None = ещё не проверяли
_fish_checked_at = 0.0


def reset() -> None:
    """Забыть кэш доступности Fish Speech и референса (тесты)."""
    global _reference_bytes, _fish_available, _fish_checked_at
    _reference_bytes, _fish_available, _fish_checked_at = None, None, 0.0


def backend() -> str:
    """Каким голосом сейчас синтезируется: fish, edge или unknown (ещё не проверяли): для состояния модуля."""
    return "unknown" if _fish_available is None else ("fish" if _fish_available else "edge")


def _get_reference() -> bytes | None:
    global _reference_bytes
    if _reference_bytes is None and FISH_REFERENCE.exists():
        _reference_bytes = FISH_REFERENCE.read_bytes()
        logger.info(f"Референс GLaDOS: {FISH_REFERENCE} ({len(_reference_bytes):,} б)")
    return _reference_bytes


def _clean(text: str) -> str:
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'`[^`]*`', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return text
    # Обрезаем по границе предложения, а не посреди слова
    cut = text[:MAX_CHARS]
    last_stop = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return cut[:last_stop + 1] if last_stop > MAX_CHARS // 2 else cut


def _pcm_to_wav(pcm: bytes, sample_rate: int = FISH_SR,
                channels: int = 1, sampwidth: int = 2) -> bytes:
    """Обернуть raw PCM int16 в WAV-файл."""
    data_size   = len(pcm)
    byte_rate   = sample_rate * channels * sampwidth
    block_align = channels * sampwidth
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", data_size + 36, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, sampwidth * 8,
        b"data", data_size,
    )
    return header + pcm


def _tmp_path(suffix: str, prefix: str, directory: Path | None) -> str:
    """Имя нового временного файла (создаётся пустым и закрывается): в папке модуля или в системной временной."""
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix, prefix=prefix, dir=str(directory) if directory is not None else None)
    os.close(fd)
    return name


# ── Fish Speech синтез ─────────────────────────────────────────────────────────

async def _fish_ping() -> bool:
    """Проверить доступность Fish Speech."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{FISH_URL}/v1/health")
            return r.status_code < 500
    except Exception:
        return False


async def _fish_synth(text: str, directory: Path | None = None) -> str | None:
    """Синтезировать текст через Fish Speech → временный WAV. Вернуть путь или None."""
    import ormsgpack  # lazy import

    ref = _get_reference()
    payload: dict = {
        "text":      text,
        "streaming": True,
        "format":    "wav",
    }
    if ref:
        payload["references"]      = [{"audio": ref, "text": ""}]
        payload["use_memory_cache"] = "on"

    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=FISH_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{FISH_URL}/v1/tts",
                content=ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_NUMPY),
                headers={"Content-Type": "application/msgpack"},
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(4096):
                    if chunk:
                        chunks.append(chunk)
    except Exception as e:
        logger.warning(f"Fish Speech ошибка: {e}")
        return None

    raw_pcm = b"".join(chunks)
    if not raw_pcm:
        return None

    # Fish Speech при streaming=True отдаёт raw PCM без заголовка
    wav = _pcm_to_wav(raw_pcm)
    tmp = _tmp_path(".wav", "glados_fish_", directory)
    with open(tmp, "wb") as f:
        f.write(wav)
    return tmp


# ── edge-tts fallback ─────────────────────────────────────────────────────────

async def _edge_synth(text: str, directory: Path | None = None) -> str:
    """Синтезировать текст через edge-tts → MP3."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=VOICE, pitch=PITCH, rate=RATE)
    tmp = _tmp_path(".mp3", "glados_edge_", directory)
    await communicate.save(tmp)
    return tmp


def _synthesize(clean: str, directory: Path | None = None) -> str | None:
    """Синтезировать один сегмент текста в аудиофайл (Fish Speech → fallback edge-tts)."""
    global _fish_available, _fish_checked_at

    now = time.monotonic()
    if _fish_available is None or (not _fish_available and now - _fish_checked_at >= FISH_RECHECK_S):
        was = _fish_available
        _fish_available = asyncio.run(_fish_ping())
        _fish_checked_at = now
        if _fish_available:
            logger.info("Fish Speech доступен — использую голос GLaDOS")
        elif was is None:
            logger.info("Fish Speech недоступен — fallback на edge-tts")

    if _fish_available:
        path = asyncio.run(_fish_synth(clean, directory))
        if path is None:
            path = asyncio.run(_edge_synth(clean, directory))  # разовая ошибка — пробуем edge-tts
        return path
    return asyncio.run(_edge_synth(clean, directory))


def synth_file(text: str, directory: Path | None = None) -> str | None:
    """Синтезировать текст в аудиофайл (Fish Speech, запасной edge-tts) БЕЗ воспроизведения. Блокирующий вызов (гонять
    через asyncio.to_thread). directory - куда класть файл (по умолчанию временная папка системы). Файл потом удалить самому;
    забытые удаляет чистка временной папки модуля. Ошибка синтеза: None."""
    clean = _clean(text)
    if not clean:
        return None
    try:
        return _synthesize(clean, directory)
    except Exception as e:
        logger.warning(f"Синтез не удался: {type(e).__name__}: {e}")
        return None
