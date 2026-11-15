"""HTTP-приложение сервиса распознавания (aiohttp).

    POST /v1/transcribe?lang=ru   тело: сырой PCM s16le 16000 Гц моно (application/octet-stream)
        200 {"text", "language", "duration_ms", "engine", "took_ms"}
        400 мусор на входе (пусто, нечётная длина, короче 0,2 с, неверный язык, язык не поддержан движком)
        413 длиннее 60 с (или тело больше лимита)
        503 перегруз (очередь полна) или модель ещё не загружена; заголовок Retry-After
        504 не уложились в STT_TIMEOUT_S (вместе с ожиданием в очереди)
        500 движок упал на этом звуке
    GET /health     200 {"ok": true, "engine", "model_loaded": true, "queue"} | 503 {"ok": false, ..., "error"}
    GET /v1/info    движок, модель, язык, потоки, лимиты

Распознавание блокирующее: идёт в пуле из STT_WORKERS потоков; сверх них до STT_QUEUE запросов ждут очереди, остальные получают 503.
Модель открывается ОДИН раз в фоне сразу после старта сервера (не блокируя его): до этого /health отвечает 503 и распознавание
тоже 503 с Retry-After (клиент ядра повторов не делает, реплика устаревает). Если веса не нашлись, открытие повторяется каждые
STT_RETRY_S секунд, поэтому после `python -m stt.fetch` перезапуск не нужен. Прогрев: одна секунда тишины сразу после открытия.
Текст реплик в журнал не пишется: только длина звука и время.
"""
import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from aiohttp import web

from .config import BYTES_PER_SAMPLE, MAX_AUDIO_S, MAX_BODY_BYTES, MIN_AUDIO_S, MIN_BODY_BYTES, SAMPLE_RATE, SttConfig
from .engine import Engine, EngineUnavailable, pcm_to_float

logger = logging.getLogger("stt.app")

_LANG = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")


def _error(status: int, code: str, detail: str, *, retry_after: int | None = None) -> web.Response:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return web.json_response({"error": code, "detail": detail}, status=status, headers=headers)


class SttService:
    def __init__(self, config: SttConfig, engine: Engine):
        self.config, self.engine = config, engine
        self.pool = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="stt")
        self._sem: asyncio.Semaphore | None = None       # создаётся в цикле событий (on_startup)
        self.running = 0                                 # занимают поток пула
        self.waiting = 0                                 # ждут свободного потока
        self.load_error = ""
        self.ready: asyncio.Event | None = None         # создаётся в start()
        self._load_task: asyncio.Task | None = None

    # ------------------------------------------------------------------------------------ жизненный цикл
    async def start(self, app: web.Application | None = None) -> None:
        self._sem = asyncio.Semaphore(self.config.workers)
        self.ready = asyncio.Event()
        self._load_task = asyncio.create_task(self._load_loop())

    async def stop(self, app: web.Application | None = None) -> None:
        if self._load_task is not None:
            self._load_task.cancel()
            await asyncio.gather(self._load_task, return_exceptions=True)
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def _load_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.engine.load)
            except EngineUnavailable as e:
                self.load_error = str(e)
                logger.warning("Модель не открылась (скачивание без разрешения не делаю), повтор через %g с: %s",
                               self.config.retry_s, self.load_error)
                await asyncio.sleep(self.config.retry_s)
                continue
            except Exception as e:                                       # неожиданное: не убиваем сервер, пробуем позже
                self.load_error = f"{type(e).__name__}: {e}"[:200]
                logger.exception("Открытие модели упало")
                await asyncio.sleep(self.config.retry_s)
                continue
            break
        self.load_error = ""
        if self.config.warmup:
            try:
                await asyncio.to_thread(self.engine.transcribe, np.zeros(SAMPLE_RATE, dtype=np.float32), self.config.language)
            except Exception as e:
                logger.info("Прогрев не удался (не страшно): %s", type(e).__name__)
        logger.info("Распознавание готово: движок %s, модель %s", self.engine.name, self.engine.model)
        self.ready.set()

    @property
    def model_loaded(self) -> bool:
        return self.engine.loaded and self.ready is not None and self.ready.is_set()

    # ------------------------------------------------------------------------------------ распознавание
    async def _recognize(self, audio: np.ndarray, lang: str) -> str:
        """Ждать свободного потока и распознать. Занятость потока снимается, когда поток ДЕЙСТВИТЕЛЬНО закончил
        (даже если запрос уже отвалился по таймауту): иначе потоков стало бы больше, чем STT_WORKERS."""
        self.waiting += 1
        try:
            await self._sem.acquire()
        finally:
            self.waiting -= 1
        self.running += 1
        fut = asyncio.get_running_loop().run_in_executor(self.pool, self.engine.transcribe, audio, lang)

        def _done(f: asyncio.Future) -> None:
            self.running -= 1
            self._sem.release()
            if not f.cancelled():
                f.exception()                       # забрать исключение, чтобы не было «never retrieved»

        fut.add_done_callback(_done)
        return await asyncio.shield(fut)

    async def transcribe(self, request: web.Request) -> web.Response:
        if not self.model_loaded:
            return _error(503, "model_not_loaded", self.load_error or "модель ещё загружается", retry_after=5)
        lang = (request.query.get("lang") or self.config.language).strip().lower()
        if not _LANG.match(lang):
            return _error(400, "bad_lang", "параметр lang: код языка вроде ru")
        if not self.engine.supports(lang):
            return _error(400, "unsupported_lang", f"движок {self.engine.name} не умеет язык {lang}")
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            return _error(413, "too_long", f"звук длиннее {MAX_AUDIO_S:g} с")
        # перегруз проверяем до чтения тела: лишний звук не копим в памяти
        if self.running >= self.config.workers and self.waiting >= self.config.queue:
            return _error(503, "overloaded", "очередь распознавания полна", retry_after=1)
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return _error(413, "too_long", f"звук длиннее {MAX_AUDIO_S:g} с")
        if len(body) > MAX_BODY_BYTES:
            return _error(413, "too_long", f"звук длиннее {MAX_AUDIO_S:g} с")
        if not body or len(body) % BYTES_PER_SAMPLE:
            return _error(400, "bad_pcm", "тело должно быть PCM s16le (чётное число байт), не пустое")
        if len(body) < MIN_BODY_BYTES:
            return _error(400, "too_short", f"звук короче {MIN_AUDIO_S:g} с")
        audio = pcm_to_float(body)
        duration_ms = int(len(audio) * 1000 / SAMPLE_RATE)
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(self._recognize(audio, lang), timeout=self.config.timeout_s)
        except asyncio.TimeoutError:
            return _error(504, "timeout", f"распознавание не уложилось в {self.config.timeout_s:g} с")
        except EngineUnavailable as e:
            return _error(503, "model_not_loaded", str(e), retry_after=5)
        except Exception as e:
            logger.exception("Движок упал на звуке %d мс", duration_ms)
            return _error(500, "engine_error", type(e).__name__)
        took_ms = int((time.monotonic() - started) * 1000)
        logger.info("распознано: звук %d мс, символов %d, %d мс", duration_ms, len(text), took_ms)
        return web.json_response({"text": text, "language": lang, "duration_ms": duration_ms,
                                  "engine": self.engine.name, "took_ms": took_ms})

    # ------------------------------------------------------------------------------------ служебное
    async def health(self, request: web.Request) -> web.Response:
        body = {"ok": self.model_loaded, "engine": self.engine.name, "model_loaded": self.model_loaded, "queue": self.waiting}
        if not self.model_loaded:
            body["error"] = self.load_error or "модель загружается"
            return web.json_response(body, status=503)
        return web.json_response(body)

    async def info(self, request: web.Request) -> web.Response:
        c = self.config
        return web.json_response({
            "engine": self.engine.name, "model": self.engine.model, "model_loaded": self.model_loaded,
            "requested_engine": c.engine, "language": c.language,
            "languages": sorted(self.engine.languages) if self.engine.languages else None,
            "threads": c.threads, "workers": c.workers, "queue_limit": c.queue, "timeout_s": c.timeout_s,
            "running": self.running, "queue": self.waiting,
            "sample_rate": SAMPLE_RATE, "min_audio_s": MIN_AUDIO_S, "max_audio_s": MAX_AUDIO_S,
        })


STT_KEY: "web.AppKey[SttService]" = web.AppKey("stt")


def create_app(config: SttConfig, engine: Engine) -> web.Application:
    service = SttService(config, engine)
    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app[STT_KEY] = service
    app.router.add_post("/v1/transcribe", service.transcribe)
    app.router.add_get("/health", service.health)
    app.router.add_get("/v1/info", service.info)
    app.on_startup.append(service.start)
    app.on_cleanup.append(service.stop)
    return app
