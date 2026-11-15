"""Облачный синтез через OpenRouter (Fish Audio S2.1 Pro) с запасным локальным ESpeech.

    POST https://openrouter.ai/api/v1/audio/speech       Authorization: Bearer <OPENROUTER_API_KEY>
    {"model": "fish-audio/s2.1-pro", "input": <текст>, "response_format": "pcm",
     "input_references": [{"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64,<WAV референса>"}},
                          {"type": "text", "text": "<дословный текст референса>"}]}
    -> сырой PCM s16le, `Content-Type: audio/pcm;rate=44100;channels=1` (частота и каналы читаются из заголовка)

Что покидает машину: текст предложения, референс голоса персонажа и его текст. Ключ и референс нигде не пишутся (ни в журнал, ни в
состояние, ни в тексты ошибок: `scrub`); в журнале только длины.

`FallbackTtsClient`: на каждое предложение сначала облако, при любой ошибке (таймаут, сеть, 4xx/5xx, 402/429) предложение
озвучивается локальным ESpeech. Облако не используется, если: сработал предохранитель (две неудачи подряд выключают его на 60 с,
дальше вдвое дольше до 10 минут, успех сбрасывает), исчерпан суточный лимит символов (до 00:00 UTC), в предложении личные слова
(`leaks`): такое предложение уходит только локально.
"""
import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp
from gits_sdk.voice.audio import resample

from .frames import SAMPLE_RATE
from .tempo import time_stretch
from .tts import (
    MAX_WAV_BYTES,
    TtsBadAudio,
    TtsClient,
    TtsError,
    TtsServerError,
    TtsSettings,
    TtsTimeout,
    TtsUnavailable,
    clean_text,
    read_reference,
)

logger = logging.getLogger("voice_engine.cloud_tts")

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "fish-audio/s2.1-pro"
USAGE_FILE = "tts_cloud_usage.json"
BREAKER_FAILURES = 2                    # столько неудач подряд выключают облако
BREAKER_FIRST_S = 60.0
BREAKER_MAX_S = 600.0
_RATE = re.compile(r"rate\s*=\s*(\d+)", re.I)
_CHANNELS = re.compile(r"channels\s*=\s*(\d+)", re.I)
_BEARER = re.compile(r"(Bearer\s+)[\w.\-~+/=]+", re.I)
_KEY_LIKE = re.compile(r"sk-[\w\-]{8,}")


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Вычистить из чужого текста (ответ сервиса, сообщение исключения) ключ, референс и всё, похожее на токен."""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 6:
            out = out.replace(secret, "***")
    out = _BEARER.sub(r"\1***", out)
    return _KEY_LIKE.sub("***", out)


def parse_pcm_format(content_type: str) -> tuple[int, int]:
    """`audio/pcm;rate=44100;channels=1` -> (44100, 1). Без частоты или с чужим типом это TtsBadAudio: ничего не угадываем."""
    if not content_type.lower().startswith(("audio/pcm", "audio/l16")):
        raise TtsBadAudio("ответ облака не PCM")
    rate, channels = _RATE.search(content_type), _CHANNELS.search(content_type)
    if rate is None:
        raise TtsBadAudio("в ответе облака нет частоты PCM")
    rate_v, ch_v = int(rate.group(1)), int(channels.group(1)) if channels else 1
    if not 8000 <= rate_v <= 192000 or ch_v not in (1, 2):
        raise TtsBadAudio("частота или число каналов PCM вне допустимого")
    return rate_v, ch_v


def pcm_to_16k(pcm: bytes, rate: int, channels: int, tempo: float) -> bytes:
    """Сырой PCM облака -> PCM 16 кГц моно с темпом. Блокирующая работа: звать через `asyncio.to_thread`."""
    pcm = pcm[:len(pcm) - len(pcm) % (2 * channels)]
    if not pcm:
        raise TtsBadAudio("в ответе облака нет звука")
    out = resample(pcm, rate, SAMPLE_RATE, channels, 1) if (rate, channels) != (SAMPLE_RATE, 1) else pcm
    return time_stretch(out, tempo)


class OpenRouterTtsClient:
    def __init__(self, api_key: str, settings: TtsSettings, *, model: str = DEFAULT_MODEL, tempo: float = 1.0,
                 connect_timeout_s: float = 5.0, timeout_s: float = 12.0, base_url: str = OPENROUTER_URL):
        self._key = api_key
        self.settings, self.model, self.tempo = settings, model, tempo
        self.connect_timeout_s, self.timeout_s = connect_timeout_s, timeout_s
        self.url = f"{base_url.rstrip('/')}/audio/speech"
        self._http: aiohttp.ClientSession | None = None
        self._ref: tuple[str, str] | None = None             # (data-URI референса, его текст)

    def _secrets(self) -> tuple[str, ...]:
        return (self._key, self._ref[0] if self._ref else "")

    async def _reference(self) -> tuple[str, str]:
        if self._ref is None:
            wav, text = await read_reference(self.settings)
            self._ref = ("data:audio/wav;base64," + base64.b64encode(wav).decode("ascii"), text)
        return self._ref

    async def synthesize(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> bytes:
        text = clean_text(text)
        if not text:
            raise TtsError("пустой текст")
        ref_uri, ref_text = await self._reference()
        payload = {"model": self.model, "input": text, "response_format": "pcm",
                   "input_references": [{"type": "input_audio", "input_audio": {"data": ref_uri}}, {"type": "text", "text": ref_text}]}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        if self._http is None:
            self._http = aiohttp.ClientSession()
        timeout = aiohttp.ClientTimeout(total=self.timeout_s, connect=self.connect_timeout_s, sock_connect=self.connect_timeout_s)
        try:
            async with self._http.post(self.url, data=json.dumps(payload), headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:300]
                    raise TtsServerError(resp.status, scrub(detail, self._secrets())[:200])
                rate, channels = parse_pcm_format(resp.headers.get("Content-Type", ""))
                # тело читаем целиком кусками: `content.read(n)` отдаёт только уже пришедшую часть (звук обрывался)
                body = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    body += chunk
                    if len(body) > MAX_WAV_BYTES:
                        raise TtsBadAudio("ответ слишком большой")
        except asyncio.TimeoutError:
            raise TtsTimeout(f"облачный синтез не ответил за {self.timeout_s:g} с") from None
        except aiohttp.ClientError as e:
            raise TtsUnavailable(f"облачный синтез недоступен: {type(e).__name__}") from None
        logger.info("Облачный синтез: текст %d симв., звук %d байт", len(text), len(body))
        return await asyncio.to_thread(pcm_to_16k, bytes(body), rate, channels, self.tempo)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


class CloudBudget:
    """Суточный учёт облака: символы, отправленные сегодня (UTC), и оценка стоимости. Хранится в файле памяти (атомарная запись)."""

    def __init__(self, path: Path | None, daily_chars: int = 200000, usd_per_char: float = 0.000015,
                 now: Callable[[], float] = time.time):
        self.path, self.daily_chars, self.usd_per_char, self._now = path, daily_chars, usd_per_char, now
        self.day = self._today()
        self.chars = 0
        self._load()

    def _today(self) -> str:
        return datetime.fromtimestamp(self._now(), timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("date") == self.day:
                self.chars = max(0, int(data.get("chars", 0)))
        except (OSError, ValueError, TypeError):
            pass

    def _roll(self) -> None:
        today = self._today()
        if today != self.day:
            self.day, self.chars = today, 0

    def exhausted(self) -> bool:
        self._roll()
        return self.daily_chars > 0 and self.chars >= self.daily_chars

    @property
    def usd(self) -> float:
        return self.chars * self.usd_per_char

    async def add(self, chars: int) -> None:
        self._roll()
        self.chars += chars
        await asyncio.to_thread(self._save, {"date": self.day, "chars": self.chars})

    def _save(self, data: dict) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tts_usage_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Счётчик облачного синтеза не записан (%s)", type(e).__name__)


class FallbackTtsClient:
    """Облако на каждое предложение, при любой неудаче или запрете то же предложение локально, без паузы для слушателя."""

    def __init__(self, primary: TtsClient, fallback: TtsClient, *, budget: CloudBudget,
                 leaks: Callable[[str], Awaitable[bool] | bool] | None = None, provider: str = "openrouter",
                 model: str = DEFAULT_MODEL, clock: Callable[[], float] = time.monotonic):
        self.primary, self.fallback, self.budget, self._leaks = primary, fallback, budget, leaks
        self.provider, self.model, self._clock = provider, model, clock
        self.failures = 0
        self.backoff_s = 0.0
        self.disabled_until = 0.0
        self.counts = {"cloud": 0, "local": 0, "fallbacks": 0, "privacy_local": 0}
        self.last_error = ""

    # ── решения ──────────────────────────────────────────────────────────────────

    def disabled_reason(self) -> str:
        """Почему облако сейчас не используется (пусто: можно)."""
        if self.budget.exhausted():
            return "суточный лимит символов исчерпан (до 00:00 UTC)"
        left = self.disabled_until - self._clock()
        if left > 0:
            return f"предохранитель: облако выключено ещё на {int(left)} с после неудач"
        return ""

    @property
    def cloud_active(self) -> bool:
        return not self.disabled_reason()

    def _success(self) -> None:
        self.failures, self.backoff_s, self.disabled_until = 0, 0.0, 0.0

    def _failure(self, error: Exception) -> None:
        self.failures += 1
        status = getattr(error, "status", "")
        self.last_error = f"{type(error).__name__}{' ' + str(status) if status else ''}"
        if self.failures >= BREAKER_FAILURES:
            self.backoff_s = BREAKER_FIRST_S if not self.backoff_s else min(self.backoff_s * 2, BREAKER_MAX_S)
            self.disabled_until = self._clock() + self.backoff_s
            logger.warning("Облачный синтез выключен на %d с (%s)", int(self.backoff_s), self.last_error)

    async def _has_private_words(self, text: str) -> bool:
        if self._leaks is None:
            return False
        try:
            found = self._leaks(text)
            return bool(await found) if asyncio.iscoroutine(found) or isinstance(found, asyncio.Future) else bool(found)
        except Exception:                                   # фильтр сломан: безопаснее не отправлять в облако
            logger.warning("Фильтр личных слов не ответил: предложение озвучивается локально")
            return True

    # ── синтез ───────────────────────────────────────────────────────────────────

    async def synthesize_ex(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> tuple[bytes, str]:
        """(PCM, кем озвучено: `openrouter` или `local`)."""
        text = clean_text(text)
        if not self.disabled_reason():
            if await self._has_private_words(text):
                self.counts["privacy_local"] += 1
            else:
                await self.budget.add(len(text))             # считаем отправленные символы, даже если запрос не удастся
                try:
                    pcm = await self.primary.synthesize(text, voice=voice, emotion=emotion)
                except TtsError as e:
                    self._failure(e)
                    self.counts["fallbacks"] += 1
                else:
                    self._success()
                    self.counts["cloud"] += 1
                    return pcm, "openrouter"
        pcm = await self.fallback.synthesize(text, voice=voice, emotion=emotion)
        self.counts["local"] += 1
        return pcm, "local"

    async def synthesize(self, text: str, *, voice: str | None = None, emotion: str | None = None) -> bytes:
        return (await self.synthesize_ex(text, voice=voice, emotion=emotion))[0]

    def state(self) -> dict[str, Any]:
        reason = self.disabled_reason()
        return {"provider": self.provider, "model": self.model, "active": "local" if reason else "openrouter",
                "chars_today": self.budget.chars, "usd_today": round(self.budget.usd, 4), "daily_chars": self.budget.daily_chars,
                "cloud": self.counts["cloud"], "local": self.counts["local"], "fallbacks": self.counts["fallbacks"],
                "privacy_local": self.counts["privacy_local"], "disabled_reason": reason, "last_error": self.last_error}

    async def aclose(self) -> None:
        await self.primary.aclose()
        await self.fallback.aclose()
