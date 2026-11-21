"""Клиент общего контейнера распознавания речи `stt` (services/stt/README.md): `POST /v1/transcribe?lang=ru`, тело сырой PCM
s16le 16 кГц моно. Свой маленький клиент, а не voice_engine ядра: модуль монтажа независим от кода ядра.

Пословных времён у сервиса нет, только текст куска. 503 с `Retry-After` (очередь полна, модель не загружена) повторяется;
400 («мусор», тишина) не ошибка: кусок пропускается пустым текстом; всё остальное (сети нет, таймаут, 5xx) это `SttUnavailable`.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable

ENV_URL = "GITS_STT_URL"
DEFAULT_URL = "http://stt:8772"
PCM_RATE = 16000
PCM_BYTES_PER_S = PCM_RATE * 2
MAX_RETRY_WAIT_S = 10.0

# (статус, тело ответа JSON или None, Retry-After в секундах или None)
PostFn = Callable[[bytes], Awaitable[tuple[int, dict | None, float | None]]]


class SttUnavailable(Exception):
    """Сервис недоступен или не справляется: распознавание речи пропускается целиком."""


class HttpStt:
    def __init__(self, url: str | None = None, *, lang: str = "ru", timeout_s: float = 45.0, max_retries: int = 6,
                 post: PostFn | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.url = (url or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")
        self.lang, self.timeout_s, self.max_retries = lang, timeout_s, max_retries
        self._post = post or self._http_post
        self._sleep = sleep
        self._session = None

    async def _http_post(self, pcm: bytes) -> tuple[int, dict | None, float | None]:
        import aiohttp
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(f"{self.url}/v1/transcribe", params={"lang": self.lang}, data=pcm,
                                          headers={"Content-Type": "application/octet-stream"},
                                          timeout=aiohttp.ClientTimeout(total=self.timeout_s, connect=4)) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                try:
                    wait = float(resp.headers.get("Retry-After") or 0) or None
                except ValueError:
                    wait = None
                return resp.status, body if isinstance(body, dict) else None, wait
        except asyncio.TimeoutError:
            raise SttUnavailable("stt не ответил вовремя") from None
        except (aiohttp.ClientError, OSError) as e:
            raise SttUnavailable(f"stt недоступен: {type(e).__name__}") from None

    async def transcribe(self, pcm: bytes) -> str:
        """Текст куска; пустая строка, если сервис счёл звук мусором или тишиной (400)."""
        for attempt in range(self.max_retries + 1):
            status, body, wait = await self._post(pcm)
            if status == 200:
                text = (body or {}).get("text")
                return text.strip() if isinstance(text, str) else ""
            if status == 400:
                return ""
            if status == 503 and attempt < self.max_retries:
                await self._sleep(min(MAX_RETRY_WAIT_S, wait or 2.0))
                continue
            raise SttUnavailable("очередь stt занята" if status == 503 else f"stt ответил кодом {status}")
        raise SttUnavailable("stt не ответил")        # недостижимо при max_retries >= 0, но цикл формально может кончиться

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
