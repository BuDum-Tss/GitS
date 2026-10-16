"""Шина модулей (ТЗ 02, раздел 5): WebSocket-сервер `/module/ws` в приложении дашборда (порт 8766).

Модуль подключается сам (клиент SDK `gits_sdk`), проходит рукопожатие по ТЗ 01 §5 и дальше шлёт события, речь,
состояние, задачи и запросы к ядру, а ядро вызывает его инструменты и рассылает темы (`presence`, `settings`, `profile`,
`module`, `battery`). Поведение сервера повторяет эталон `gits_sdk.testing.FakeCore`.

Защита: только петля (`127.0.0.1`, `::1`, иначе HTTP 403) и токен `Authorization: Bearer ...` (иначе 401) до апгрейда до
WebSocket. Токен ядро создаёт при каждом старте и пишет в `%LOCALAPPDATA%\\GitS\\module_token`; в журналы он не попадает,
как и содержимое сообщений (в журнал идут тип и размер).

Входящие сообщения-уведомления модуля (`event`, `say`, `emotion`, `state`, `job.update`) обрабатываются по порядку в
отдельной задаче соединения, запросы (`core.*`) - каждый в своей задаче: цикл чтения кадров никогда не ждёт долгих дел.
"""
import asyncio
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import WSMsgType, web

import events_log
import fast_brain
import settings
from gits_sdk import contract
from gits_sdk import protocol as P
from gits_sdk.protocol import Envelope, Manifest, ProtocolError

logger = logging.getLogger("hub")

CORE_VERSION = "2026.10.0"
HELLO_TIMEOUT_S = 10.0
PRESENCE_MIN_INTERVAL_S = 0.1          # CORE-5: тема presence не чаще 10 раз в секунду
INBOX_LIMIT = 1000                     # очередь уведомлений одного модуля; дальше отбрасываются
WATCH_INTERVAL_S = 1.0                 # как часто сверяются настройки, включённость и батарейка
BATTERY_EVERY = 5                      # батарейка проверяется раз в столько тиков
BATTERY_STEP = 5                       # процентов: тема battery при изменении на столько и более
RESERVED_IDS = ("core",)               # id модулей, которые модули занять не могут
RESERVED_SETTINGS = ("enabled",)       # имя в modules.<id>.*: включённость модуля


class ModuleError(Exception):
    """Ошибка запроса к модулю (или от ядра модулю): code по MOD-9, message по-русски."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code if code in P.ERROR_CODES else "internal"
        self.message = message


def write_token_atomic(token: str, path: Path | None = None) -> Path:
    """Записать токен в файл модулей атомарно: временный файл и os.replace (CORE-1)."""
    path = path or P.token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(token, encoding="utf-8")
    os.replace(tmp, path)
    return path


def _normalize_remote(remote: str | None) -> str | None:
    if remote and remote.lower().startswith("::ffff:"):
        return remote[7:]              # IPv4 в двойном стеке: ::ffff:127.0.0.1
    return remote


class ModuleConnection:
    """Подключённый (прошедший рукопожатие) модуль."""

    def __init__(self, ws: web.WebSocketResponse, hello: dict, spec: Manifest):
        self.ws = ws
        self.module_id: str = hello["module_id"]
        self.instance: str = hello["instance"]
        self.pid: int = hello["pid"]
        self.manifest: dict = hello["manifest"]
        self.spec = spec
        self.connected_at = time.time()
        self.closed = False
        self.close_reason: str | None = None
        self.last_settings: dict | None = None        # что уже отправлено темой settings
        self.last_enabled: bool | None = None         # что уже отправлено темой module
        self.warned_kinds: set[str] = set()
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task] = set()
        self.inbox: asyncio.Queue = asyncio.Queue(INBOX_LIMIT)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send_envelope(self, env: Envelope) -> None:
        if self.closed or self.ws.closed:
            raise ModuleError("unavailable", "модуль отключён")
        try:
            text = env.dumps()
        except ProtocolError as e:
            raise ModuleError("internal", str(e)) from e
        async with self._send_lock:
            try:
                await self.ws.send_str(text)
            except (ConnectionError, RuntimeError) as e:
                raise ModuleError("unavailable", "модуль отключился") from e
        logger.debug(f"-> {self.module_id}: {env.type} ({len(text)} Б)")

    async def send(self, msg_type: str, body: dict | None = None, re: str | None = None) -> None:
        """Отправить сообщение без ожидания ответа."""
        await self.send_envelope(Envelope(msg_type, dict(body or {}), re=re))

    async def request(self, msg_type: str, body: dict | None = None, timeout: float = 30.0) -> Any:
        """Запрос модулю и ожидание `result`. ModuleError(code, message) при ошибке, тайм-ауте и обрыве."""
        env = Envelope(msg_type, dict(body or {}))
        fut = asyncio.get_running_loop().create_future()
        self._pending[env.id] = fut
        try:
            await self.send_envelope(env)
            try:
                reply = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                raise ModuleError("timeout", f"модуль не ответил за {timeout:g} с") from None
        finally:
            self._pending.pop(env.id, None)
        if reply.body.get("ok"):
            return reply.body.get("value")
        err = reply.body.get("error") if isinstance(reply.body.get("error"), dict) else {}
        raise ModuleError(str(err.get("code") or "internal"), str(err.get("message") or "модуль вернул ошибку без пояснения"))

    def resolve(self, env: Envelope) -> None:
        fut = self._pending.get(env.re or "")
        if fut is not None and not fut.done():
            fut.set_result(env)

    def fail_pending(self, message: str = "модуль отключился") -> None:
        """Ожидающие запросы завершаются ошибкой unavailable (CORE-3)."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ModuleError("unavailable", message))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_reason = self.close_reason or reason
        if not self.ws.closed:
            try:
                await asyncio.wait_for(self.ws.close(code=code, message=reason.encode()), 3.0)
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):
                pass


class _JobQuestion:
    """Вопрос задачи: пока пользователь не ответил, повторный job.update с тем же question_id второй вопрос не порождает."""

    def __init__(self):
        self.task: asyncio.Task | None = None
        self.answer: Any = None
        self.ready = False
        self.sent = False


class ModuleHub:
    def __init__(self, modules, *, attention=None, requests=None, token: str | None = None, token_file: Path | None = None,
                 settings_source: Callable[[], dict] = settings.load,
                 profile_source: Callable[[], dict] = lambda: {"id": "assistant", "title": "Ассистент", "focus_modules": []},
                 battery_source: Callable[[], dict] = lambda: {},
                 presence=None, hello_timeout: float = HELLO_TIMEOUT_S, monotonic: Callable[[], float] = time.monotonic):
        """modules - ModuleRegistry; attention - менеджер внимания (submit, submit_speech, submit_question,
        on_job_update); requests - обработчик core.* (core_requests.CoreRequests); *_source - откуда брать настройки,
        профиль и батарейку (в тестах подменяются); presence - PresenceTracker (снимок для новых модулей и эмоции)."""
        self.modules = modules
        self.attention = attention
        self.requests = requests
        if requests is not None:
            requests.hub = self                        # core.tts передаётся модулю голоса через шину (VOICE-16)
        self.token = token or P.new_token()
        self.token_file = token_file
        self._settings_source = settings_source
        self._profile_source = profile_source
        self._battery_source = battery_source
        self.presence = presence
        self.hello_timeout = hello_timeout
        self._monotonic = monotonic
        self._conns: dict[str, ModuleConnection] = {}
        self._job_questions: dict[tuple[str, str, str], _JobQuestion] = {}
        self._tasks: set[asyncio.Task] = set()
        self._watch_task: asyncio.Task | None = None
        self._battery_sent: dict | None = None
        self._presence_latest: dict | None = None
        self._presence_last = float("-inf")
        self._presence_timer: asyncio.TimerHandle | None = None
        modules.hub = self

    # ── жизненный цикл ─────────────────────────────────────────────────────────

    def attach(self, app: web.Application) -> None:
        """Маршрут GET /module/ws в приложение дашборда."""
        app.router.add_get("/module/ws", self.handle)

    async def start(self) -> None:
        """Токен в файл (атомарно) и фоновая сверка настроек, включённости и батарейки."""
        path = await asyncio.to_thread(write_token_atomic, self.token, self.token_file)
        logger.info(f"Шина модулей: токен записан в {path}")
        self._watch_task = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
        if self._presence_timer is not None:
            self._presence_timer.cancel()
        for conn in list(self._conns.values()):
            await conn.close(1001, "ядро останавливается")
        for task in list(self._tasks):
            task.cancel()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── доступ ──────────────────────────────────────────────────────────────

    def connection(self, module_id: str) -> ModuleConnection | None:
        conn = self._conns.get(module_id)
        return conn if conn is not None and not conn.closed else None

    def is_connected(self, module_id: str) -> bool:
        return self.connection(module_id) is not None

    def connections(self) -> list[ModuleConnection]:
        return [c for c in self._conns.values() if not c.closed]

    # ── рукопожатие ─────────────────────────────────────────────────────────────

    async def handle(self, request: web.Request) -> web.StreamResponse:
        remote = _normalize_remote(request.remote)
        if remote not in P.CORE_HOST_ALLOWED:                                    # MOD-3
            logger.warning("Подключение модуля не с петлевого адреса отклонено")
            raise web.HTTPForbidden()
        given = request.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode("utf-8"), f"Bearer {self.token}".encode("utf-8")):    # MOD-4
            logger.warning("Подключение модуля с неверным токеном отклонено")
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=P.HEARTBEAT_S, max_msg_size=P.MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        conn = await self._handshake(ws)
        if conn is None:
            return ws
        reason = "соединение закрыто"
        try:
            reader = conn.spawn(self._inbox_loop(conn))
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        self._on_text(conn, msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        logger.warning(f"Модуль {conn.module_id}: двоичный кадр отброшен (протокол текстовый)")
                    elif msg.type == WSMsgType.ERROR:
                        reason = f"ошибка соединения: {ws.exception()}"
                        break
                if ws.close_code and ws.close_code != 1000:
                    reason = f"закрыто с кодом {ws.close_code}"
            finally:
                reader.cancel()
        finally:
            await self._disconnected(conn, conn.close_reason or reason)
        return ws

    async def _reject(self, ws: web.WebSocketResponse, hello_id: str | None, module_id: str, reason: str) -> None:
        events_log.log("module", f"модуль {module_id} отклонён: {reason}", module=module_id, reject=True)
        logger.warning(f"Модуль {module_id} отклонён: {reason}")
        try:
            await ws.send_str(Envelope("reject", {"reason": reason}, re=hello_id).dumps())
        except (ConnectionError, RuntimeError):
            pass
        await ws.close()

    def _extra_problems(self, hello: dict) -> list[str]:
        """Проверки ядра сверх схемы: зарезервированные имена и единственность voice.output (MOD-18)."""
        m, mid = hello["manifest"], hello["module_id"]
        problems = []
        if mid in RESERVED_IDS:
            problems.append(f"идентификатор «{mid}» зарезервирован ядром")
        for name in RESERVED_SETTINGS:
            if name in (m.get("settings_schema") or {}):
                problems.append(f"settings_schema.{name}: имя зарезервировано ядром")
        return problems

    async def _handshake(self, ws: web.WebSocketResponse) -> ModuleConnection | None:
        try:
            msg = await ws.receive(timeout=self.hello_timeout)
        except asyncio.TimeoutError:
            logger.warning("Модуль не прислал hello вовремя, соединение закрыто")
            events_log.log("module", "соединение закрыто: hello не пришёл за 10 с", reject=True)
            await ws.close(code=1008, message=b"hello timeout")
            return None
        if msg.type != WSMsgType.TEXT:
            await ws.close()
            return None
        try:
            env = Envelope.loads(msg.data)
        except ProtocolError as e:
            await self._reject(ws, None, "?", f"неверное сообщение (протокол): {e}")
            return None
        if env.type != "hello":
            await self._reject(ws, env.id, "?", "до welcome допустим только hello (протокол)")
            return None
        body = env.body
        mid = str(body.get("module_id") or "?")
        if body.get("protocol") != P.PROTOCOL:
            await self._reject(ws, env.id, mid, f"protocol {body.get('protocol')} не поддерживается")
            return None
        problems = P.validate_hello(body)
        if not problems:
            problems = self._extra_problems(body)
        if problems:
            await self._reject(ws, env.id, mid, "манифест не прошёл проверку: " + "; ".join(problems))
            return None
        if "voice.output" in (body["manifest"].get("capabilities") or []):
            for other in self.connections():
                if other.module_id != mid and "voice.output" in (other.manifest.get("capabilities") or []):
                    # без слов про протокол и манифест: модуль будет повторять попытки (MOD-12)
                    await self._reject(ws, env.id, mid, f"голосовой вывод уже занят модулем «{other.module_id}»")
                    return None
        spec = Manifest.from_dict(body["manifest"])
        for warning in spec.panel_warnings:              # TZ 15: неверный panel не отклоняет модуль
            logger.warning(f"Модуль {mid}: {warning}")
        conn = ModuleConnection(ws, body, spec)
        old = self._conns.get(mid)
        if old is not None:
            old.close_reason = "вытеснен новым подключением"
            await old.close(P.CLOSE_REPLACED, "replaced")                      # MOD-13
        cfg = await asyncio.to_thread(self._settings_source)
        enabled = self.modules.is_enabled(mid, cfg)
        welcome = {"protocol": P.PROTOCOL, "core_version": CORE_VERSION, "enabled": enabled,
                   "settings": self._shared(spec, cfg), "module_settings": self._module_settings(mid, spec, cfg),
                   "profile": self._profile_source(), "battery": self._battery_payload(self._battery_source())}
        self._conns[mid] = conn
        conn.last_settings = {"settings": welcome["settings"], "module_settings": welcome["module_settings"]}
        conn.last_enabled = enabled
        self.modules.on_connected(conn)
        await conn.send_envelope(Envelope("welcome", welcome, re=env.id))
        events_log.log("module", f"модуль {mid} подключён (версия {spec.version}, pid {conn.pid})", module=mid,
                       instance=conn.instance, enabled=enabled)
        logger.info(f"Модуль {mid} подключён")
        if self.presence is not None and "presence" in spec.subscribe:
            self._spawn(self._safe_send_topic(conn, "presence", self.presence.snapshot()))
        return conn

    # ── настройки модуля ────────────────────────────────────────────────────────

    @staticmethod
    def _shared(spec: Manifest, cfg: dict) -> dict:
        return {k: cfg[k] for k in spec.shared_settings if k in cfg}

    def _module_settings(self, module_id: str, spec: Manifest, cfg: dict) -> dict:
        schema = spec.settings_schema
        stored = (cfg.get("modules") or {}).get(module_id)
        stored = stored if isinstance(stored, dict) else {}
        return {name: stored.get(name, s["default"]) for name, s in schema.items()}

    @staticmethod
    def _battery_payload(status: dict) -> dict:
        return {"percent": round(float(status.get("level", 1.0)) * 100), "sleeping": bool(status.get("sleeping", False))}

    # ── приём сообщений ─────────────────────────────────────────────────────────

    _NOTES = ("event", "say", "emotion", "state", "job.update", "panel.set", "log")

    def _on_text(self, conn: ModuleConnection, data: str) -> None:
        try:
            env = Envelope.loads(data)
        except ProtocolError as e:
            logger.warning(f"Модуль {conn.module_id}: кадр отброшен: {e}")
            return
        logger.debug(f"<- {conn.module_id}: {env.type} ({len(data)} симв.)")
        if env.type == "result":
            conn.resolve(env)
        elif env.type == "hello":
            logger.warning(f"Модуль {conn.module_id}: повторный hello отброшен")
        elif env.type in self._NOTES:
            try:
                conn.inbox.put_nowait(env)
            except asyncio.QueueFull:
                logger.warning(f"Модуль {conn.module_id}: очередь сообщений переполнена, «{env.type}» отброшено")
        elif env.type in P.CORE_REQUESTS or env.type.startswith("core.") or P.is_request(env.type):
            conn.spawn(self._handle_request(conn, env))
        else:
            logger.warning(f"Модуль {conn.module_id}: неизвестный тип сообщения «{env.type}» проигнорирован")      # MOD-10

    async def _inbox_loop(self, conn: ModuleConnection) -> None:
        while True:
            env = await conn.inbox.get()
            try:
                await self._on_note(conn, env)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Модуль {conn.module_id}: сообщение «{env.type}» не обработано: {type(e).__name__}: {e}")

    def _enabled(self, conn: ModuleConnection) -> bool:
        return self.modules.is_enabled(conn.module_id)

    async def _on_note(self, conn: ModuleConnection, env: Envelope) -> None:
        mid, body, t = conn.module_id, env.body, env.type
        if t == "state":
            summary, data = body.get("summary"), body.get("data", {})
            if not isinstance(summary, str) or len(summary) > P.STATE_SUMMARY_MAX or not isinstance(data, dict):
                logger.warning(f"Модуль {mid}: state неверный (summary строка до {P.STATE_SUMMARY_MAX}, data объект)")
                return
            self.modules.set_state(mid, summary, data)
            return
        if t == "panel.set":                           # TZ 15: блоки пространства модуля
            if not self._enabled(conn):
                return
            rec = self.modules.get(mid)
            actions = set((rec.panel or {}).get("actions") or []) if rec is not None else set()
            problems = contract.validate("panel.set", body, actions=actions)
            if problems:
                logger.warning(f"Модуль {mid}: panel.set неверен: {'; '.join(problems)}")
                return
            if not self.modules.set_panel(mid, body):
                logger.debug(f"Модуль {mid}: panel.set отброшен (не объявлено panel или чаще раза в секунду)")
            return
        if t == "job.update":
            problems = contract.validate("job.update", body)
            if problems:
                logger.warning(f"Модуль {mid}: job.update неверный: {'; '.join(problems)}")
                return
            await self._on_job_update(conn, body)
            return
        if t == "log":                                 # ТЗ 31: запись модуля в журнал (вместо прямого events_log)
            problems = contract.validate("log", body)
            if problems:
                logger.warning(f"Модуль {mid}: log неверный: {'; '.join(problems)}")
                return
            extra = {k: v for k, v in (body.get("data") or {}).items() if k not in ("kind", "text")}
            events_log.log_nowait(body["kind"], body.get("text", ""), **extra)
            return
        if not self._enabled(conn):                    # MOD-14: выключенный модуль событий и речи не шлёт
            logger.debug(f"Модуль {mid} выключен: «{t}» отброшено")
            return
        if t == "event":
            problems = contract.validate("event", body)
            if problems:
                logger.warning(f"Модуль {mid}: event неверный: {'; '.join(problems)}")
                return
            kind = body["kind"]
            if not kind.startswith(mid + "."):
                logger.warning(f"Модуль {mid}: событие «{kind}» с чужим видом отброшено")
                return
            if kind not in {e.kind for e in conn.spec.emits} and kind not in conn.warned_kinds:
                conn.warned_kinds.add(kind)
                logger.warning(f"Модуль {mid}: событие «{kind}» не объявлено в emits")
            if self.attention is not None:
                await self.attention.submit(mid, body)
        elif t == "say":
            problems = contract.validate("say", body)
            if problems:
                logger.warning(f"Модуль {mid}: say неверный: {'; '.join(problems)}")
                return
            if self.attention is not None:
                await self.attention.submit_speech(mid, body)
        elif t == "emotion":
            emotion = body.get("emotion")
            if emotion in fast_brain.EMOTIONS:
                fast_brain.publish(f"module:{mid}", {"emotion": emotion, "action": "none", "source": mid})
                if self.presence is not None:
                    self.presence.update(emotion=emotion)
            else:
                logger.warning(f"Модуль {mid}: неизвестная эмоция {emotion!r}")

    # ── задачи ──────────────────────────────────────────────────────────────────

    async def _on_job_update(self, conn: ModuleConnection, body: dict) -> None:
        mid, job_id = conn.module_id, body["job_id"]
        self.modules.update_job(mid, body)
        if body["state"] in P.JOB_FINISHED:
            for key in [k for k in self._job_questions if k[0] == mid and k[1] == job_id]:
                entry = self._job_questions.pop(key)
                if entry.task is not None and not entry.task.done():
                    entry.task.cancel()                # вопрос уже не нужен: задача закончилась
        if not self._enabled(conn):
            return
        if body["state"] == "waiting_user":
            await self._job_question(conn, body)
        if self.attention is not None:
            await self.attention.on_job_update(mid, body)

    async def _job_question(self, conn: ModuleConnection, job: dict) -> None:
        """Вопрос задачи (MOD-20): один раз на question_id, ответ уходит модулю как job.answer."""
        q, mid = job["question"], conn.module_id
        key = (mid, job["job_id"], q["question_id"])
        entry = self._job_questions.get(key)
        if entry is not None:
            if entry.ready and not entry.sent:         # после переподключения модуль повторил job.update: ответ уже есть
                await self._send_answer(key, entry)
            return
        entry = _JobQuestion()
        self._job_questions[key] = entry
        entry.task = self._spawn(self._ask_job_question(key, entry, q))

    async def _ask_job_question(self, key: tuple[str, str, str], entry: _JobQuestion, q: dict) -> None:
        from attention import Question
        mid, job_id, question_id = key
        question = Question(kind=q["kind"], question=q["question"], options=q.get("options"),
                            timeout_s=q.get("timeout_s", P.ASK_TIMEOUT_DEFAULT), source=mid,
                            context={"job_id": job_id, **(q.get("context") or {})},
                            urgency_hint=q.get("urgency_hint"), job_id=job_id, question_id=question_id)
        try:
            answer, _by = await self.attention.submit_question(question)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Вопрос задачи {job_id} модуля {mid} не задан: {e}")
            self._job_questions.pop(key, None)
            return
        entry.answer, entry.ready = answer, True
        await self._send_answer(key, entry)

    async def _send_answer(self, key: tuple[str, str, str], entry: _JobQuestion) -> None:
        mid, job_id, question_id = key
        conn = self.connection(mid)
        if conn is None:
            return                                     # отправим при следующем job.update этой задачи после переподключения
        try:
            await conn.send("job.answer", {"job_id": job_id, "question_id": question_id, "answer": entry.answer})
            entry.sent = True
        except ModuleError as e:
            logger.warning(f"job.answer для {mid}/{job_id} не отправлен: {e.message}")

    async def answer_job(self, module_id: str, job_id: str, question_id: str, answer: Any) -> str | None:
        """Ответ на вопрос задачи не словами, а кнопкой в дашборде: вопрос снимается из очереди approvals, модулю уходит
        job.answer. Возвращает причину отказа или None."""
        rec = self.modules.get(module_id)
        job = rec.jobs.get(job_id) if rec is not None else None
        q = job.get("question") if job and job.get("state") == "waiting_user" else None
        if not q:
            return "у задачи нет открытого вопроса"
        if question_id and question_id != q["question_id"]:
            return "это уже другой вопрос"
        kind = q["kind"]
        if kind == "approval" and not isinstance(answer, bool):
            return "ответ должен быть true или false"
        if kind == "choice" and answer not in (q.get("options") or []):
            return f"ответ должен быть одним из {q.get('options')}"
        if kind == "text" and not (isinstance(answer, str) and answer.strip()):
            return "ответ должен быть непустой строкой"
        key = (module_id, job_id, q["question_id"])
        entry = self._job_questions.get(key)
        if entry is None:
            entry = self._job_questions[key] = _JobQuestion()
        if entry.sent:
            return "на вопрос уже ответили"
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()                          # вопрос в очереди approvals больше не нужен
        entry.answer, entry.ready = answer, True
        await self._send_answer(key, entry)
        return None if entry.sent or self.connection(module_id) is None else "ответ не отправлен"

    # ── запросы модуля к ядру ───────────────────────────────────────────────────

    async def _handle_request(self, conn: ModuleConnection, env: Envelope) -> None:
        try:
            if env.type not in P.CORE_REQUESTS:
                raise ModuleError("unknown", f"ядро не знает запрос «{env.type}»")
            if not self._enabled(conn):
                raise ModuleError("unavailable", "модуль выключен")
            if self.requests is None:
                raise ModuleError("unavailable", "запросы к ядру не подключены")
            reply = P.result_ok(env.id, await self.requests.handle(conn, env.type, env.body))
        except ModuleError as e:
            reply = P.result_error(env.id, e.code, e.message)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Запрос {env.type} от модуля {conn.module_id}")
            reply = P.result_error(env.id, "internal", f"сбой ядра при обработке запроса: {type(e).__name__}")
        try:
            try:
                await conn.send_envelope(reply)
            except ModuleError as e:
                if e.code != "internal":
                    raise
                await conn.send_envelope(P.result_error(env.id, "internal", "ответ не удалось отправить: " + e.message))
        except ModuleError:
            logger.debug(f"Ответ на {env.type} модулю {conn.module_id} не доставлен: модуль отключился")

    # ── отключение ──────────────────────────────────────────────────────────────

    async def _disconnected(self, conn: ModuleConnection, reason: str) -> None:
        conn.closed = True
        conn.fail_pending()
        for task in list(conn.tasks):
            task.cancel()
        current = self._conns.get(conn.module_id) is conn
        if current:
            del self._conns[conn.module_id]
            self.modules.on_disconnected(conn.module_id, reason)
        events_log.log("module", f"модуль {conn.module_id} отключился: {reason}", module=conn.module_id,
                       replaced=not current)
        logger.info(f"Модуль {conn.module_id} отключился: {reason}")

    # ── темы ────────────────────────────────────────────────────────────────────

    async def _safe_send_topic(self, conn: ModuleConnection, topic: str, data: dict) -> None:
        try:
            await conn.send("topic", {"topic": topic, "data": data})
        except ModuleError:
            pass

    async def send_topic(self, module_id: str, topic: str, data: dict) -> None:
        """Тема одному модулю (если подключён)."""
        conn = self.connection(module_id)
        if conn is not None:
            await self._safe_send_topic(conn, topic, data)

    async def broadcast(self, topic: str, data: dict) -> None:
        """Тема всем подключённым и включённым модулям, подписанным на неё (CORE-5)."""
        targets = [c for c in self.connections() if topic in c.spec.subscribe and self._enabled(c)]
        await asyncio.gather(*[self._safe_send_topic(c, topic, data) for c in targets])

    def notify(self, topic: str, data: dict) -> None:
        """Как broadcast, но из синхронного кода (смена профиля, присутствие): рассылка уходит фоновой задачей."""
        coro = self.broadcast(topic, data)
        try:
            self._spawn(coro)
        except RuntimeError:
            coro.close()                               # цикла событий нет (старт или тесты): некому рассылать; корутину закрываем


    def broadcast_presence(self, data: dict) -> None:
        """Тема presence не чаще 10 раз в секунду; в паузе побеждает последнее значение (CORE-5)."""
        self._presence_latest = data
        if self._presence_timer is not None:
            return
        wait = PRESENCE_MIN_INTERVAL_S - (self._monotonic() - self._presence_last)
        if wait <= 0:
            self._flush_presence()
            return
        try:
            self._presence_timer = asyncio.get_running_loop().call_later(wait, self._flush_presence)
        except RuntimeError:
            pass

    def _flush_presence(self) -> None:
        self._presence_timer = None
        data, self._presence_latest = self._presence_latest, None
        if data is not None:
            self._presence_last = self._monotonic()
            self.notify("presence", data)

    async def refresh(self) -> None:
        """Сверить настройки и включённость сейчас (после изменения из дашборда); тема уходит только при отличии."""
        self.modules.invalidate()
        cfg = await asyncio.to_thread(self._settings_source)
        for conn in self.connections():
            mid = conn.module_id
            enabled = self.modules.is_enabled(mid, cfg)
            if enabled != conn.last_enabled:
                conn.last_enabled = enabled
                await self._safe_send_topic(conn, "module", {"enabled": enabled})
            payload = {"settings": self._shared(conn.spec, cfg),
                       "module_settings": self._module_settings(mid, conn.spec, cfg)}
            if payload != conn.last_settings:
                conn.last_settings = payload
                if "settings" in conn.spec.subscribe:
                    await self._safe_send_topic(conn, "settings", payload)

    async def _watch(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            tick += 1
            try:
                await self.refresh()
                if tick % BATTERY_EVERY == 0:
                    await self._check_battery()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Сверка настроек модулей: {e}")

    async def _check_battery(self) -> None:
        now = self._battery_payload(await asyncio.to_thread(self._battery_source))
        last = self._battery_sent
        if last is None or now["sleeping"] != last["sleeping"] or abs(now["percent"] - last["percent"]) >= BATTERY_STEP:
            self._battery_sent = now
            if last is not None:
                await self.broadcast("battery", now)

