"""Синтез речи: интерфейс `TtsClient`, заглушка для тестов и клиент существующего контейнера синтеза (ESpeech, F5-TTS).

API контейнера (`services/f5-tts/f5_server.py`, образ `docker/espeech`):

    GET  {TTS_URL}/health   -> 200, когда модель загружена (503, пока грузится)
    POST {TTS_URL}/tts      JSON {"text", "ref_audio_b64", "ref_text", "speed"} -> audio/wav (моно, 24 кГц, PCM 16 или float)

Клиент превращает ответ в PCM 16 кГц моно (формат протокола), ресемплируя в потоке-исполнителе: цикл событий не занят.
Референс голоса персонажа (аудио и дословный текст) задаётся настройками и читается один раз.
"""
import asyncio
from collections.abc import Callable
import base64
import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aiohttp
import numpy as np
from gits_sdk.voice.audio import resample

from .frames import BYTES_PER_MS, SAMPLE_RATE

logger = logging.getLogger("voice_engine.tts")

MAX_WAV_BYTES = 32 * 1024 * 1024
MAX_TTS_CHARS = 800


class TtsError(Exception):
    """Любая ошибка синтеза."""


class TtsTimeout(TtsError):
    """Сервис не ответил за отведённое время."""


class TtsUnavailable(TtsError):
    """Сервис недоступен."""


class TtsServerError(TtsError):
    """Сервис ответил кодом не 200."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"сервис синтеза ответил {status}" + (f": {detail}" if detail else ""))
        self.status = status


class TtsBadAudio(TtsError):
    """Ответ не разобрать как WAV."""


class TtsClient(Protocol):
    async def synthesize(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> bytes:
        """Текст -> PCM s16le 16 кГц моно целиком."""

    async def aclose(self) -> None: ...


def clean_text(text: str, limit: int = MAX_TTS_CHARS) -> str:
    """Текст для озвучки: без разметки Markdown, одним пробелом, не длиннее limit (по границе предложения)."""
    text = re.sub(r"\*+|#+\s*|`[^`]*`", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:stop + 1] if stop > limit // 2 else cut


@dataclass(frozen=True)
class TtsSettings:
    url: str = ""
    ref_audio: str = ""            # WAV референса голоса персонажа (читается один раз)
    ref_text: str = ""             # его дословный текст
    # () -> (путь к WAV, текст) референса активного персонажа; зовётся при первом синтезе, если ref_audio не задан настройкой
    ref_resolver: Callable[[], tuple[Path, str] | None] | None = None
    speed: float = 1.0
    voice: str = ""                # необязательные: передаются сервису, если он их понимает (ESpeech их игнорирует)
    emotion: str = ""
    timeout_s: float = 60.0


def decode_wav(data: bytes) -> tuple[bytes, int]:
    """WAV (PCM 16 бит, PCM 8/24/32 бит или float32, моно или стерео) -> (PCM s16le моно, частота)."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TtsBadAudio("ответ не WAV")
    fmt = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body = data[pos + 8:pos + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            fmt = struct.unpack_from("<HHIIHH", body)
        elif chunk_id == b"data":
            if fmt is None:
                raise TtsBadAudio("в WAV нет описания формата")
            tag, channels, rate, _, _, bits = fmt
            if tag == 0xFFFE:                # WAVE_FORMAT_EXTENSIBLE: настоящий тип в подформате
                tag = 3 if bits == 32 else 1
            return _to_mono16(body, tag, channels, bits), rate
        pos += 8 + size + (size & 1)
    raise TtsBadAudio("в WAV нет данных")


def _to_mono16(raw: bytes, tag: int, channels: int, bits: int) -> bytes:
    if channels not in (1, 2):
        raise TtsBadAudio(f"каналов в WAV {channels}, поддержаны 1 и 2")
    if tag == 1 and bits == 16:
        arr = np.frombuffer(raw[:len(raw) - len(raw) % 2], dtype="<i2").astype(np.float32)
    elif tag == 1 and bits == 8:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) * 256.0
    elif tag == 1 and bits == 32:
        arr = np.frombuffer(raw[:len(raw) - len(raw) % 4], dtype="<i4").astype(np.float32) / 65536.0
    elif tag == 3 and bits == 32:
        arr = np.frombuffer(raw[:len(raw) - len(raw) % 4], dtype="<f4").astype(np.float32) * 32767.0
    else:
        raise TtsBadAudio(f"формат WAV не поддержан (тип {tag}, {bits} бит)")
    if channels == 2:
        arr = arr[:len(arr) - len(arr) % 2].reshape(-1, 2).mean(axis=1)
    return np.clip(np.rint(arr), -32768, 32767).astype("<i2").tobytes()


def wav_to_pcm16k(data: bytes) -> bytes:
    """Целый WAV сервиса -> PCM 16 кГц моно. Блокирующая работа: звать через `asyncio.to_thread`."""
    pcm, rate = decode_wav(data)
    return resample(pcm, rate, SAMPLE_RATE) if rate != SAMPLE_RATE else pcm


class HttpTtsClient:
    def __init__(self, settings: TtsSettings):
        self.settings = settings
        self._http: aiohttp.ClientSession | None = None
        self._ref: tuple[str, str] | None = None

    async def _reference(self) -> tuple[str, str]:
        """(WAV референса в base64, его текст): явная настройка, иначе референс активного персонажа. ESpeech без референса не
        говорит, поэтому его отсутствие это ошибка конфигурации, а не «голос по умолчанию»."""
        if self._ref is None:
            cfg = self.settings
            path = Path(cfg.ref_audio) if cfg.ref_audio else None
            text = cfg.ref_text
            if (path is None or not path.is_file()) and cfg.ref_resolver is not None:
                found = await asyncio.to_thread(cfg.ref_resolver)
                if found is not None:
                    path, text = found
            if path is None or not path.is_file():
                raise TtsError("нет референса голоса: положи espeech_ref.wav и espeech_ref.txt в папку voice/ персонажа "
                               "или задай GITS_TTS_REF_AUDIO и GITS_TTS_REF_TEXT")
            self._ref = (base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode("ascii"), text)
        return self._ref

    async def synthesize(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> bytes:
        text = clean_text(text)
        if not text:
            raise TtsError("пустой текст")
        cfg = self.settings
        ref_b64, ref_text = await self._reference()
        payload = {"text": text, "ref_audio_b64": ref_b64, "ref_text": ref_text, "speed": cfg.speed}
        if voice or cfg.voice:
            payload["voice"] = voice or cfg.voice
        if emotion or cfg.emotion:
            payload["emotion"] = emotion or cfg.emotion
        if self._http is None:
            self._http = aiohttp.ClientSession()
        try:
            async with self._http.post(f"{cfg.url.rstrip('/')}/tts", json=payload,
                                       timeout=aiohttp.ClientTimeout(total=cfg.timeout_s)) as resp:
                if resp.status != 200:
                    raise TtsServerError(resp.status, (await resp.text())[:200])
                data = await resp.content.read(MAX_WAV_BYTES + 1)
        except asyncio.TimeoutError:
            raise TtsTimeout(f"сервис синтеза не ответил за {cfg.timeout_s:g} с") from None
        except aiohttp.ClientError as e:
            raise TtsUnavailable(f"сервис синтеза недоступен: {type(e).__name__}") from None
        if len(data) > MAX_WAV_BYTES:
            raise TtsBadAudio("ответ слишком большой")
        return await asyncio.to_thread(wav_to_pcm16k, data)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


class FakeTts:
    """Синтез для тестов: тон длиной `ms_per_char` мс на символ; записывает тексты, может падать."""

    def __init__(self, ms_per_char: int = 40, *, delay_s: float = 0.0, fail: Exception | None = None):
        self.ms_per_char, self.delay_s, self.fail = ms_per_char, delay_s, fail
        self.calls: list[str] = []

    async def synthesize(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> bytes:
        text = clean_text(text)
        self.calls.append(text)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        return b"\x10\x00" * (len(text) * self.ms_per_char * BYTES_PER_MS // 2)

    async def aclose(self) -> None:
        pass
