"""Распознавание речи: интерфейс `SttClient`, заглушка для тестов и клиент HTTP-сервиса (отдельный контейнер).

API сервиса распознавания (его пишет и держит другой исполнитель, описание в docs/VOICE_ENGINE.md):

    POST {STT_URL}/v1/transcribe?lang=ru
    Content-Type: application/octet-stream        тело: сырой PCM s16le, 16000 Гц, моно
    -> 200 application/json {"text": "...", "language": "ru", "duration_ms": 1830}

Повторов нет: реплика устаревает быстро, ответ «нет» лучше запоздалого «да». Каждая ошибка это типизированное исключение.
"""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import aiohttp

from .frames import pcm_ms

logger = logging.getLogger("voice_engine.stt")

MAX_STT_BYTES = 60 * 16000 * 2        # не больше минуты звука за запрос


class SttError(Exception):
    """Любая ошибка распознавания."""


class SttTimeout(SttError):
    """Сервис не ответил за отведённое время."""


class SttUnavailable(SttError):
    """Сервис недоступен: соединение не установлено или оборвано."""


class SttServerError(SttError):
    """Сервис ответил кодом не 200."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"сервис распознавания ответил {status}" + (f": {detail}" if detail else ""))
        self.status = status


class SttBadResponse(SttError):
    """Ответ не по контракту (не JSON, нет поля text)."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = ""
    duration_ms: int = 0


class SttClient(Protocol):
    async def transcribe(self, pcm: bytes, *, lang: str | None = None) -> Transcript: ...

    async def aclose(self) -> None: ...


class HttpSttClient:
    def __init__(self, base_url: str, *, timeout_s: float = 15.0, lang: str = "ru", token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.lang = lang
        self._token = token
        self._http: aiohttp.ClientSession | None = None

    async def transcribe(self, pcm: bytes, *, lang: str | None = None) -> Transcript:
        if not pcm or len(pcm) > MAX_STT_BYTES:
            raise SttError("звук пустой или длиннее минуты")
        if self._http is None:
            self._http = aiohttp.ClientSession()
        headers = {"Content-Type": "application/octet-stream"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            async with self._http.post(f"{self.base_url}/v1/transcribe", params={"lang": lang or self.lang}, data=pcm,
                                       headers=headers, timeout=aiohttp.ClientTimeout(total=self.timeout_s)) as resp:
                if resp.status != 200:
                    raise SttServerError(resp.status, (await resp.text())[:200])
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    raise SttBadResponse("ответ не JSON") from None
        except asyncio.TimeoutError:
            raise SttTimeout(f"сервис распознавания не ответил за {self.timeout_s:g} с") from None
        except aiohttp.ClientError as e:
            raise SttUnavailable(f"сервис распознавания недоступен: {type(e).__name__}") from None
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise SttBadResponse("в ответе нет текстового поля text")
        duration = body.get("duration_ms")
        return Transcript(text=body["text"].strip(), language=str(body.get("language") or ""),
                          duration_ms=int(duration) if isinstance(duration, (int, float)) and not isinstance(duration, bool)
                          else pcm_ms(pcm))

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None


class FakeStt:
    """Распознаватель для тестов: отдаёт заготовленные тексты по очереди (или `default`), записывает вызовы."""

    def __init__(self, texts: list[str] | None = None, *, default: str = "тест", delay_s: float = 0.0,
                 fail: Exception | None = None):
        self._texts = deque(texts or [])
        self.default, self.delay_s, self.fail = default, delay_s, fail
        self.calls: list[tuple[int, str | None]] = []

    async def transcribe(self, pcm: bytes, *, lang: str | None = None) -> Transcript:
        self.calls.append((pcm_ms(pcm), lang))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail is not None:
            raise self.fail
        text = self._texts.popleft() if self._texts else self.default
        return Transcript(text=text, language=lang or "ru", duration_ms=pcm_ms(pcm))

    async def aclose(self) -> None:
        pass
