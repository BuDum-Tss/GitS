"""Клиент модуля ГЛаДОС: `ModuleClient` (ТЗ 01, раздел 10).

Модуль создаёт клиент с манифестом, регистрирует обработчики инструментов и вызывает `await client.run()`: клиент сам
подключается к ядру по WebSocket, проходит рукопожатие, принимает вызовы, переподключается при обрыве. Журналы идут в
`logging.getLogger("glados_module")`, токен в них не попадает.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from . import protocol as P
from .protocol import Envelope, ManifestError, ProtocolError, ToolError

log = logging.getLogger("glados_module")

ASK_GRACE_S = 30.0               # MOD-25: ждём ответа на ask столько секунд сверх timeout_s
HANDSHAKE_TIMEOUT_S = 10.0       # сколько ждём welcome после hello
SHUTDOWN_HOOK_TIMEOUT_S = 8.0    # хук on_shutdown должен уложиться (ядро убивает процесс через 10 с)
JOB_PROGRESS_INTERVAL_S = 2.0    # MOD-19: прогресс задачи не чаще раза в 2 с
FINISHED_JOBS_KEPT = 20          # job.list: незавершённые и последние 20 завершённых

# причины reject, после которых переподключаться бессмысленно (MOD-12)
_FATAL_REJECT_WORDS = ("протокол", "protocol", "манифест", "manifest")

Handler = Callable[..., Awaitable[Any]]


class ModuleClient:
    """Клиент модуля. Имена методов зафиксированы ТЗ 01, раздел 10."""

    def __init__(self, manifest: dict, *, url: str | None = None, token: str | None = None):
        problems = P.validate_manifest(manifest)
        if problems:
            raise ManifestError("манифест не прошёл проверку: " + "; ".join(problems), problems)
        self.manifest = copy.deepcopy(manifest)
        # включённость приходит темой module (MOD-14): подписываемся всегда, иначе enabled не обновится
        subscribe = list(self.manifest.get("subscribe") or [])
        if "module" not in subscribe:
            subscribe.append("module")
        self.manifest["subscribe"] = subscribe
        self.module_id: str = self.manifest["id"]
        self.instance = P.new_instance()
        self._url = url
        self._token = token

        # состояние, которое присылает ядро
        self.settings: dict = {}
        self.module_settings: dict = {}
        self.enabled: bool = True
        self.profile: dict = {}
        self.battery: dict = {}
        self.core_version: str | None = None

        # настраиваемые параметры (для тестов)
        self.reconnect_delays = P.RECONNECT_DELAYS
        self.job_progress_interval = JOB_PROGRESS_INTERVAL_S

        # задачи модуля: последнее состояние каждой (для job.list и повторной отправки, MOD-22)
        self.jobs: dict[str, dict] = {}

        self._tools: dict[str, Handler] = {}
        self._checks: dict[str, Handler] = {}
        self._topics: dict[str, list[Handler]] = {}
        self._requests: dict[str, Handler] = {}
        self._job_answer: Handler | None = None
        self._job_cancel: Handler | None = None
        self._on_reconnect: Handler | None = None
        self._on_shutdown: Handler | None = None

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._gen = 0                       # номер соединения: ответы старого соединения новому не шлём
        self._welcomes = 0
        self._pending: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()       # рукопожатие пройдено, связь есть
        self._exit_code = 0
        self._last_state: dict | None = None
        self._job_sent: dict[str, tuple[dict, float]] = {}

    # ------------------------------------------------------------ регистрация обработчиков

    def _tool_names(self) -> set[str]:
        return {t["name"] for t in self.manifest.get("tools") or []}

    def tool(self, name: str):
        """Обработчик инструмента из манифеста: `async (args: dict) -> JSON-значение`."""
        if name not in self._tool_names():
            raise ValueError(f"инструмента «{name}» нет в манифесте модуля «{self.module_id}»")

        def deco(fn: Handler) -> Handler:
            self._tools[name] = fn
            return fn
        return deco

    def check(self, name: str):
        """Обработчик `tool.check` (для `danger_check: true`): `async (args) -> str | None` (причина опасности)."""
        if name not in self._tool_names():
            raise ValueError(f"инструмента «{name}» нет в манифесте модуля «{self.module_id}»")

        def deco(fn: Handler) -> Handler:
            self._checks[name] = fn
            return fn
        return deco

    def on_topic(self, topic: str):
        """Обработчик темы ядра: `async (data: dict) -> None`. Тема должна быть в `subscribe` манифеста."""
        if topic not in P.TOPICS:
            raise ValueError(f"неизвестная тема «{topic}», допустимо {list(P.TOPICS)}")
        if topic not in self.manifest["subscribe"]:
            raise ValueError(f"тема «{topic}» не указана в subscribe манифеста")

        def deco(fn: Handler) -> Handler:
            self._topics.setdefault(topic, []).append(fn)
            return fn
        return deco

    def on_request(self, msg_type: str):
        """Обработчик прочих сообщений ядра (`speak`, `speak.stop`, `tts.synthesize`, `graph.get`, `deliver`, ...):
        `async (body: dict) -> значение`. Для запросов значение уходит в `result`; ToolError становится `ok: false`.
        Обработчик на встроенный тип (`job.list`, `tool.call`) заменяет встроенное поведение."""
        def deco(fn: Handler) -> Handler:
            self._requests[msg_type] = fn
            return fn
        return deco

    def on_job_answer(self, fn: Handler) -> Handler:
        """`async (job_id, question_id, answer) -> None`: ответ пользователя на вопрос задачи."""
        self._job_answer = fn
        return fn

    def on_job_cancel(self, fn: Handler) -> Handler:
        """`async (job_id) -> None`: отменить задачу (ToolError или исключение превращаются в ошибку `result`)."""
        self._job_cancel = fn
        return fn

    def on_reconnect(self, fn: Handler) -> Handler:
        """`async () -> None`: вызывается после каждого переподключения (не после первого подключения).
        Здесь модуль пересылает незавершённые задачи: `await client.resend_jobs()` (MOD-22)."""
        self._on_reconnect = fn
        return fn

    def on_shutdown(self, fn: Handler) -> Handler:
        """`async (reason: str) -> None`: ядро просит завершиться (на уборку 10 с, из них хуку отведено 8)."""
        self._on_shutdown = fn
        return fn

    def _check_handlers(self) -> None:
        missing = [t["name"] for t in self.manifest.get("tools") or [] if t["name"] not in self._tools]
        if missing:
            raise RuntimeError("нет обработчиков для инструментов манифеста: " + ", ".join(missing))
        no_check = [t["name"] for t in self.manifest.get("tools") or []
                    if t.get("danger_check") and t["name"] not in self._checks]
        if no_check:
            raise RuntimeError("для инструментов с danger_check нужен @client.check: " + ", ".join(no_check))

    # ------------------------------------------------------------ жизненный цикл

    @property
    def connected(self) -> bool:
        """Рукопожатие пройдено и соединение живо."""
        return self._ws is not None and not self._ws.closed

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Дождаться рукопожатия (для модулей, которым нужна связь до начала работы); TimeoutError по истечении."""
        await asyncio.wait_for(self._ready.wait(), timeout)

    @property
    def url(self) -> str:
        return self._url or P.core_url()

    async def _resolve_token(self) -> str | None:
        """Токен: параметр, GLADOS_MODULE_TOKEN, файл ядра (MOD-4). Файл перечитывается при каждой попытке:
        ядро меняет токен при каждом старте."""
        if self._token:
            return self._token
        env = os.environ.get(P.ENV_TOKEN)
        if env:
            return env
        return await asyncio.to_thread(P.read_token_file)

    def main(self) -> None:
        """Запуск как программы: `run()` в новом цикле и выход с кодом завершения (reject -> 2)."""
        try:
            code = asyncio.run(self.run())
        except KeyboardInterrupt:
            code = 0
        sys.exit(code)

    async def stop(self) -> None:
        """Остановить `run()` (закрыть соединение и не переподключаться)."""
        self._stop.set()
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    async def run(self) -> int:
        """Подключиться, пройти рукопожатие, обслуживать ядро. Не возвращается до shutdown или вытеснения (4000).

        Возвращает код завершения процесса: 0 (shutdown, вытеснение, stop) или 2 (reject про протокол/манифест).
        Бросает RuntimeError, если для инструмента манифеста нет обработчика.
        """
        self._check_handlers()
        self._stop.clear()
        self._exit_code = 0
        attempt = 0
        async with aiohttp.ClientSession() as session:
            try:
                while not self._stop.is_set():
                    if await self._connect_once(session):
                        attempt = 0                    # дошли до welcome: счёт задержек начинается заново
                    if self._stop.is_set():
                        break
                    delays = self.reconnect_delays
                    delay = float(delays[min(attempt, len(delays) - 1)])
                    attempt += 1
                    log.info("модуль «%s»: переподключение через %.0f с", self.module_id, delay)
                    try:
                        await asyncio.wait_for(self._stop.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
            finally:
                await self._cancel_tasks()
        return self._exit_code

    async def _cancel_tasks(self) -> None:
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _connect_once(self, session: aiohttp.ClientSession) -> bool:
        """Одно подключение. Возвращает True, если дошло до welcome (тогда задержка переподключения сбрасывается)."""
        token = await self._resolve_token()
        if not token:
            log.warning("нет токена модулей: задайте %s или запустите ядро (файл %s)", P.ENV_TOKEN, P.token_path())
            return False
        try:
            ws = await session.ws_connect(
                self.url, headers={"Authorization": f"Bearer {token}"}, heartbeat=P.HEARTBEAT_S,
                max_msg_size=P.MAX_MESSAGE_BYTES)
        except aiohttp.WSServerHandshakeError as e:
            log.warning("ядро не приняло подключение: HTTP %s", e.status)
            return False
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            log.warning("ядро недоступно (%s): %s", self.url, type(e).__name__)
            return False

        welcomed = False
        try:
            kind, payload = await self._handshake(ws)
            if kind == "reject":
                reason = str(payload)
                if any(w in reason.lower() for w in _FATAL_REJECT_WORDS):
                    log.error("ядро отклонило модуль «%s»: %s. Переподключения не будет, процесс завершается "
                              "с кодом %d.", self.module_id, reason, P.EXIT_REJECTED)
                    self._exit_code = P.EXIT_REJECTED
                    self._stop.set()
                else:
                    log.warning("ядро отклонило модуль «%s»: %s", self.module_id, reason)
                return False
            if kind != "welcome":
                log.warning("соединение закрыто до welcome")
                return False
            welcomed = True
            self._gen += 1
            gen = self._gen
            self._apply_welcome(payload.body)
            self._welcomes += 1
            self._ws = ws
            self._ready.set()
            log.info("модуль «%s» подключён к ядру %s", self.module_id, self.core_version or "")
            if self._last_state is not None:
                self._spawn(self._send_env(Envelope("state", self._last_state), gen))
            if self._welcomes > 1 and self._on_reconnect is not None:
                self._spawn(self._run_hook(self._on_reconnect, name="on_reconnect"))
            await self._receive_loop(ws, gen)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой соединения с ядром")
        finally:
            self._ws = None
            self._ready.clear()
            self._fail_pending()
            if not ws.closed:
                await ws.close()

        if ws.close_code == P.CLOSE_REPLACED:
            log.warning("модуль «%s» вытеснен новым подключением (код %d); процесс завершается",
                        self.module_id, P.CLOSE_REPLACED)
            self._stop.set()
        elif welcomed and not self._stop.is_set():
            log.warning("соединение с ядром потеряно (код %s)", ws.close_code)
        return welcomed

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> tuple[str, Any]:
        hello = Envelope("hello", P.hello_body(self.manifest, self.instance))
        await ws.send_str(hello.dumps())

        async def wait() -> tuple[str, Any]:
            while True:
                env = await self._read(ws)
                if env is None:
                    return "closed", None
                if env.type == "welcome" and env.re == hello.id:
                    return "welcome", env
                if env.type == "reject" and env.re == hello.id:
                    return "reject", env.body.get("reason", "")
                log.warning("до welcome пришло сообщение «%s»: игнорируется", env.type)

        try:
            return await asyncio.wait_for(wait(), HANDSHAKE_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("ядро не ответило на hello за %.0f с", HANDSHAKE_TIMEOUT_S)
            return "closed", None

    def _apply_welcome(self, body: dict) -> None:
        self.core_version = body.get("core_version")
        self.enabled = bool(body.get("enabled", True))
        self.settings = dict(body.get("settings") or {})
        self.module_settings = dict(body.get("module_settings") or {})
        self.profile = dict(body.get("profile") or {})
        self.battery = dict(body.get("battery") or {})

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> Envelope | None:
        """Следующее сообщение; None, если соединение закрыто. Битые кадры пропускаются."""
        while True:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    return Envelope.loads(msg.data)
                except ProtocolError as e:
                    log.warning("битое сообщение от ядра пропущено: %s", e)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                log.warning("двоичный кадр от ядра пропущен: протокол текстовый")
            else:
                return None

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse, gen: int) -> None:
        while True:
            env = await self._read(ws)
            if env is None:
                return
            self._dispatch(env, gen)

    # ------------------------------------------------------------ приём

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _dispatch(self, env: Envelope, gen: int) -> None:
        t = env.type
        if t == "result":
            self._on_result(env)
        elif t == "topic":
            self._on_topic(env.body)
        elif t == "shutdown":
            self._spawn(self._shutdown(str(env.body.get("reason", ""))))
        elif t in ("welcome", "reject"):
            log.warning("лишнее сообщение «%s» после рукопожатия: игнорируется", t)
        elif t in self._requests:
            self._spawn(self._serve(env, gen, self._requests[t], env.body))
        elif t == "tool.call":
            self._spawn(self._serve(env, gen, self._tool_call, env.body))
        elif t == "tool.check":
            self._spawn(self._serve(env, gen, self._tool_check, env.body))
        elif t == "job.cancel":
            self._spawn(self._serve(env, gen, self._job_cancel_request, env.body))
        elif t == "job.list":
            self._spawn(self._serve(env, gen, self._job_list, env.body))
        elif t == "job.answer":
            self._spawn(self._run_hook(self._job_answer_call, env.body, name="on_job_answer"))
        elif P.is_request(t):
            self._spawn(self._send_env(P.result_error(env.id, "unknown", f"модуль не обрабатывает запрос «{t}»"), gen))
        else:
            log.warning("неизвестный тип сообщения «%s» от ядра: игнорируется", t)

    def _on_result(self, env: Envelope) -> None:
        fut = self._pending.get(env.re or "")
        if fut is None:
            log.debug("ответ на неизвестный запрос %s: игнорируется", env.re)
        elif not fut.done():
            fut.set_result(env)

    def _on_topic(self, body: dict) -> None:
        topic, data = body.get("topic"), body.get("data")
        if not isinstance(topic, str) or not isinstance(data, dict):
            log.warning("тема ядра с неверным телом: игнорируется")
            return
        if topic == "settings":
            if isinstance(data.get("settings"), dict):
                self.settings = dict(data["settings"])
            if isinstance(data.get("module_settings"), dict):
                self.module_settings = dict(data["module_settings"])
        elif topic == "profile":
            self.profile = dict(data)
        elif topic == "module":
            self.enabled = bool(data.get("enabled", True))
        elif topic == "battery":
            self.battery = dict(data)
        for handler in self._topics.get(topic, ()):
            self._spawn(self._run_hook(handler, data, name=f"on_topic({topic})"))

    async def _run_hook(self, fn: Handler | None, *args, name: str = "хук") -> None:
        if fn is None:
            return
        try:
            await fn(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("сбой в обработчике %s", name)

    async def _shutdown(self, reason: str) -> None:
        log.info("ядро просит завершиться: %s", reason)
        if self._on_shutdown is not None:
            try:
                await asyncio.wait_for(self._on_shutdown(reason), SHUTDOWN_HOOK_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("on_shutdown не уложился в %.0f с", SHUTDOWN_HOOK_TIMEOUT_S)
            except Exception:
                log.exception("сбой в on_shutdown")
        await self.stop()

    async def _serve(self, env: Envelope, gen: int, fn: Handler, *args) -> None:
        """Выполнить обработчик и, если сообщение было запросом, ответить `result` (MOD-24)."""
        wants_reply = P.is_request(env.type)
        try:
            reply = P.result_ok(env.id, await fn(*args))
        except asyncio.CancelledError:
            raise
        except ToolError as e:
            log.info("обработчик «%s» отказал: %s (%s)", env.type, e.message, e.code)
            reply = P.result_error(env.id, e.code, e.message)
        except Exception as e:
            log.exception("сбой обработчика «%s»", env.type)         # с трассировкой, только в журнал модуля
            reply = P.result_error(env.id, "internal", str(e) or type(e).__name__)
        if not wants_reply:
            return
        try:
            text = reply.dumps()
        except ProtocolError as e:
            log.error("ответ на «%s» нельзя отправить: %s", env.type, e)
            text = P.result_error(env.id, "internal", str(e)).dumps()
        await self._send_text(text, gen)

    async def _tool_call(self, body: dict) -> Any:
        name = body.get("tool")
        if isinstance(name, str) and name.startswith(self.module_id + "."):
            name = name[len(self.module_id) + 1:]
        fn = self._tools.get(name) if isinstance(name, str) else None
        if fn is None:
            raise ToolError(f"у модуля «{self.module_id}» нет инструмента «{name}»", code="unknown")
        args = body.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolError("аргументы должны быть объектом", code="bad_request")
        if not self.enabled:
            raise ToolError(f"модуль «{self.module_id}» выключен", code="unavailable")
        return await fn(args)

    async def _tool_check(self, body: dict) -> dict:
        name = body.get("tool")
        fn = self._checks.get(name) if isinstance(name, str) else None
        if fn is None:
            return {"danger": None}
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        danger = await fn(args)
        return {"danger": None if danger is None else str(danger)}

    async def _job_answer_call(self, body: dict) -> None:
        if self._job_answer is None:
            log.warning("ответ на вопрос задачи пришёл, но on_job_answer не задан")
            return
        await self._job_answer(body.get("job_id"), body.get("question_id"), body.get("answer"))

    async def _job_cancel_request(self, body: dict) -> dict:
        if self._job_cancel is None:
            raise ToolError("модуль не умеет отменять задачи", code="unknown")
        await self._job_cancel(body.get("job_id"))
        return {}

    async def _job_list(self, body: dict) -> dict:
        return {"jobs": self.job_statuses()}

    # ------------------------------------------------------------ отправка

    async def _send_text(self, text: str, gen: int | None = None) -> bool:
        ws = self._ws
        if ws is None or ws.closed or (gen is not None and gen != self._gen):
            return False
        async with self._send_lock:
            try:
                await ws.send_str(text)
            except (ConnectionError, RuntimeError, aiohttp.ClientError):
                return False
        return True

    async def _send_env(self, env: Envelope, gen: int | None = None) -> bool:
        return await self._send_text(env.dumps(), gen)

    async def _notify(self, msg_type: str, body: dict) -> bool:
        """Отправить сообщение без ответа; нет связи с ядром — тихо отбросить (False)."""
        if not self.connected:
            log.debug("нет связи с ядром: «%s» отброшено", msg_type)
            return False
        return await self._send_env(Envelope(msg_type, body))

    async def event(self, kind: str, text: str, **fields) -> bool:
        """Событие для менеджера внимания (раздел 7.1). Поля: data, urgency_hint, addressed_to_me, sender,
        reply_channel, dedupe_key, expires_in_s, memorable, announce. Возвращает True, если отправлено.

        Выключенный модуль (`enabled == False`) событий не шлёт (MOD-14). Текст длиннее 1000 символов обрезается.
        """
        extra = set(fields) - {"data", "urgency_hint", "addressed_to_me", "sender", "reply_channel", "dedupe_key",
                               "expires_in_s", "memorable", "announce"}
        if extra:
            raise TypeError("неизвестные поля события: " + ", ".join(sorted(extra)))
        body = {"kind": kind, "text": P.clip(str(text), P.EVENT_TEXT_MAX), **{k: v for k, v in fields.items()
                                                                              if v is not None}}
        problems = P.validate_event(body)
        if not kind.startswith(self.module_id + "."):
            problems.append(f"kind должен начинаться с «{self.module_id}.»")
        if problems:
            raise ValueError("событие неверно: " + "; ".join(problems))
        if not self.enabled:
            log.debug("модуль выключен: событие «%s» отброшено", kind)
            return False
        return await self._notify("event", body)

    async def say(self, text: str, mode: str = "fact", **fields) -> bool:
        """Просьба сказать (раздел 7.2). Поля: emotion, urgency_hint, expires_in_s, key, announce (объявление о долгом
        запуске: звучит всегда, правило 14). Выключенный модуль молчит."""
        extra = set(fields) - {"emotion", "urgency_hint", "expires_in_s", "key", "announce"}
        if extra:
            raise TypeError("неизвестные поля say: " + ", ".join(sorted(extra)))
        body = {"text": str(text), "mode": mode, **{k: v for k, v in fields.items() if v is not None}}
        problems = P.validate_say(body)
        if problems:
            raise ValueError("say неверен: " + "; ".join(problems))
        if not self.enabled:
            log.debug("модуль выключен: say отброшен")
            return False
        return await self._notify("say", body)

    async def emotion(self, emotion: str, cause: str) -> bool:
        """Эмоция (из списка `fast_brain.EMOTIONS` ядра; допустимость проверяет ядро). Выключенный модуль молчит."""
        if not self.enabled:
            log.debug("модуль выключен: эмоция отброшена")
            return False
        return await self._notify("emotion", {"emotion": str(emotion), "cause": str(cause)})

    async def state(self, summary: str, data: dict | None = None) -> bool:
        """Сводка для дашборда и выбора профиля. Последняя пересылается ядру заново после переподключения."""
        body = {"summary": P.clip(str(summary), P.STATE_SUMMARY_MAX), "data": dict(data or {})}
        self._last_state = body
        return await self._notify("state", body)

    async def request(self, msg_type: str, body: dict | None = None, *, timeout: float = 60.0) -> Any:
        """Запрос к ядру (`core.*`): возвращает `value` ответа.

        ToolError(code=...) при `ok: false` или обрыве связи; asyncio.TimeoutError, если ответа нет `timeout` секунд.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise ToolError("нет связи с ядром", code="unavailable")
        env = Envelope(msg_type, dict(body or {}))
        text = env.dumps()
        fut = asyncio.get_running_loop().create_future()
        self._pending[env.id] = fut
        try:
            if not await self._send_text(text, self._gen):
                raise ToolError("нет связи с ядром", code="unavailable")
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(env.id, None)
        return P.unwrap_result(reply)

    def _fail_pending(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ToolError("связь с ядром потеряна", code="unavailable"))

    async def ask(self, question: str, kind: str = "approval", options: list[str] | None = None,
                  timeout_s: float = P.ASK_TIMEOUT_DEFAULT, context: dict | None = None,
                  urgency_hint: int | None = None) -> Any:
        """Вопрос пользователю (раздел 7.3). Возвращает ответ: bool для `approval`, строку для `choice` и `text`;
        по тайм-ауту ядро отвечает False / None. Ждёт `timeout_s + 30` секунд, потом asyncio.TimeoutError (MOD-25)."""
        body: dict = {"kind": kind, "question": question, "timeout_s": timeout_s}
        if options is not None:
            body["options"] = list(options)
        if context is not None:
            body["context"] = context
        if urgency_hint is not None:
            body["urgency_hint"] = urgency_hint
        problems = P.validate_ask(body)
        if problems:
            raise ValueError("вопрос неверен: " + "; ".join(problems))
        value = await self.request("core.ask", body, timeout=timeout_s + ASK_GRACE_S)
        return value.get("answer") if isinstance(value, dict) else value

    # ------------------------------------------------------------ задачи

    def job_statuses(self) -> list[dict]:
        """Незавершённые задачи и последние 20 завершённых (для `job.list`)."""
        return [copy.deepcopy(j) for j in self.jobs.values()]

    def _prune_jobs(self) -> None:
        finished = [jid for jid, j in self.jobs.items() if j["state"] in P.JOB_FINISHED]
        for jid in finished[:-FINISHED_JOBS_KEPT]:
            self.jobs.pop(jid, None)
            self._job_sent.pop(jid, None)

    async def job_update(self, job_id: str, **fields) -> bool:
        """Ход задачи (раздел 9). Поля: title, state, stage, progress, text, question, artifacts, error.

        Клиент хранит полное состояние задачи и каждый раз шлёт его целиком: достаточно передавать изменённое.
        Смена state, stage, вопроса, ошибки или артефактов уходит сразу; изменение только прогресса и текста не чаще
        раза в 2 секунды (MOD-19), иначе возвращается False. Вопрос убирается сам при выходе из waiting_user.
        """
        extra = set(fields) - {"title", "state", "stage", "progress", "text", "question", "artifacts", "error"}
        if extra:
            raise TypeError("неизвестные поля задачи: " + ", ".join(sorted(extra)))
        merged = dict(self.jobs.get(job_id, {"job_id": job_id}))
        for k, v in fields.items():
            if v is None:
                merged.pop(k, None)             # None = убрать поле
            else:
                merged[k] = v
        new_state = merged.get("state")
        if new_state != "waiting_user" and "question" not in fields:
            merged.pop("question", None)
        if new_state != "failed" and "error" not in fields:
            merged.pop("error", None)
        problems = P.validate_job_update(merged)
        if problems:
            raise ValueError("job.update неверен: " + "; ".join(problems))
        self.jobs.pop(job_id, None)
        self.jobs[job_id] = merged                      # свежие в конце
        self._prune_jobs()

        now = time.monotonic()
        last = self._job_sent.get(job_id)
        if last is not None:
            key = ("state", "stage", "question", "error", "artifacts")
            if (all(last[0].get(k) == merged.get(k) for k in key)
                    and now - last[1] < self.job_progress_interval):
                return False
        if not self.connected:
            log.debug("нет связи с ядром: job.update %s отложен до resend_jobs", job_id)
            return False
        sent = await self._notify("job.update", copy.deepcopy(merged))
        if sent:
            self._job_sent[job_id] = (copy.deepcopy(merged), now)
        return sent

    async def resend_jobs(self) -> int:
        """Заново послать ядру все незавершённые задачи (MOD-22). Звать из `@client.on_reconnect`."""
        count = 0
        for job_id, job in list(self.jobs.items()):
            if job["state"] in P.JOB_FINISHED:
                continue
            if await self._notify("job.update", copy.deepcopy(job)):
                self._job_sent[job_id] = (copy.deepcopy(job), time.monotonic())
                count += 1
        return count
