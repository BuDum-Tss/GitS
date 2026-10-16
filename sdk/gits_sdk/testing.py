"""Поддельное ядро для тестов модулей (ТЗ 01, раздел 10): `FakeCore`.

Это настоящий aiohttp-сервер на свободном порту 127.0.0.1 с адресом `/module/ws`: он проверяет токен (401), принимает
`hello`, проверяет манифест, отвечает `welcome` или `reject`, вытесняет старое подключение того же модуля (код 4000),
записывает все сообщения модуля и позволяет вызвать инструмент, послать тему или ответить на запрос модуля.

    async with FakeCore() as core:
        client = ModuleClient(MANIFEST, url=core.url, token=core.token)
        task = asyncio.create_task(client.run())
        await core.wait_connected()
        assert await core.call_tool("repeat", {"text": "привет"}) == "привет"
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from typing import Any

import aiohttp
from aiohttp import web

from . import contract
from . import protocol as P
from .protocol import Envelope, ProtocolError, ToolError

log = logging.getLogger("gits_sdk.testing")

NO_REPLY = object()   # вернуть из обработчика запроса, чтобы ядро промолчало (проверка тайм-аутов)


class _Conn:
    def __init__(self, ws: web.WebSocketResponse, transport: asyncio.Transport | None = None):
        self.ws = ws
        self.transport = transport
        self.welcomed = False
        self.module_id: str | None = None
        self.hello: dict | None = None
        self.pending: dict[str, asyncio.Future] = {}


class FakeCore:
    """Ядро в памяти. Все списки (`messages`, `hellos`, ...) пополняются по мере работы."""

    def __init__(self, *, token: str = "fake-token", enabled: bool = True, settings: dict | None = None,
                 module_settings: dict | None = None, profile: dict | None = None, battery: dict | None = None,
                 core_version: str = "fake"):
        self.token = token
        self.enabled = enabled
        self.settings: dict = {"streamer": False, "dnd": False} if settings is None else dict(settings)
        self.module_settings: dict | None = None if module_settings is None else dict(module_settings)
        self.profile: dict = profile or {"id": "assistant", "title": "Ассистент", "focus_modules": []}
        self.battery: dict = battery or {"percent": 100, "sleeping": False}
        self.core_version = core_version

        self.messages: list[Envelope] = []       # всё, что прислал модуль после рукопожатия (включая result)
        self.sent: list[Envelope] = []           # всё, что отправило ядро
        self.hellos: list[dict] = []             # тела принятых hello
        self.rejected: list[dict] = []           # тела hello, на которые ответили reject
        self.states: dict[str, dict] = {}        # последняя `state` каждого модуля
        self.panels: dict[str, dict] = {}        # последнее `panel.set` каждого модуля: {"blocks": [...]} (TZ 15)
        self.violations: list[str] = []          # нарушения протокола модулем (сообщения до welcome и т.п.)
        self.logs: list[dict] = []               # принятые сообщения `log` (запись модуля в журнал ядра, ТЗ 31)
        self.bad_messages: list[str] = []        # кадры, которые не разобрались как конверт
        self.auth_failures = 0
        self.connections = 0                     # сколько WebSocket принято
        self.conn: _Conn | None = None           # текущее подключённое (прошедшее welcome) соединение
        self.reject_next: list[str] = []         # причины для ближайших reject (по одной на hello)
        self.handlers: dict[str, Callable] = {}  # type -> (body) -> значение; для запросов модуля к ядру

        self._conns: list[_Conn] = []
        self._tasks: set[asyncio.Task] = set()
        self._runner: web.AppRunner | None = None
        self._port = 0

    # ------------------------------------------------------------ запуск

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/module/ws"

    async def __aenter__(self) -> FakeCore:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/module/ws", self._handle)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self._port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        for conn in list(self._conns):
            if not conn.ws.closed:
                await conn.ws.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def wait_for(self, predicate: Callable[[], Any], timeout: float = 5.0) -> Any:
        """Ждать, пока `predicate()` не станет истинным (опрос раз в 10 мс); asyncio.TimeoutError по истечении
        `timeout`. Условие может смотреть и на состояние самого модуля, поэтому ожидание не привязано к сообщениям."""
        async def waiter():
            while True:
                value = predicate()
                if value:
                    return value
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(waiter(), timeout)

    async def wait_connected(self, count: int = 1, timeout: float = 5.0) -> None:
        """Ждать, пока модуль не пройдёт рукопожатие `count`-й раз и будет подключён."""
        await self.wait_for(lambda: len(self.hellos) >= count and self.conn is not None, timeout)

    async def wait_disconnected(self, timeout: float = 5.0) -> None:
        await self.wait_for(lambda: self.conn is None, timeout)

    # ------------------------------------------------------------ сервер

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        if request.remote not in P.CORE_HOST_ALLOWED:
            raise web.HTTPForbidden()
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            self.auth_failures += 1
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=P.HEARTBEAT_S, max_msg_size=P.MAX_MESSAGE_BYTES)
        await ws.prepare(request)
        conn = _Conn(ws, request.transport)
        self._conns.append(conn)
        self.connections += 1
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_text(conn, msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self.violations.append("двоичный кадр: протокол текстовый")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            self._conns.remove(conn)
            if self.conn is conn:
                self.conn = None
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ToolError("модуль отключился", code="unavailable"))
        return ws

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _on_text(self, conn: _Conn, data: str) -> None:
        try:
            env = Envelope.loads(data)
        except ProtocolError as e:
            self.bad_messages.append(str(e))
            return
        if not conn.welcomed:
            if env.type != "hello":
                self.violations.append(f"до welcome пришло «{env.type}» (MOD-12)")
                return
            await self._on_hello(conn, env)
            return
        if env.type == "hello":
            self.violations.append("повторный hello на том же соединении")
            return
        self.messages.append(env)
        if env.type == "state":
            self.states[conn.module_id or "?"] = env.body
        if env.type in contract.REGISTRY and env.type in ("event", "say", "job.update", "log", "emotion", "state"):
            problems = contract.validate(env.type, env.body)          # тот же контракт, что проверяет ядро (MC-7)
            if problems:
                self.violations.append(f"{env.type} неверен: " + "; ".join(problems))
        if env.type == "log" and not contract.validate("log", env.body):
            self.logs.append(env.body)
        if env.type == "panel.set":
            hello = getattr(conn, "hello", None) or {}
            actions = set(((hello.get("manifest") or {}).get("panel") or {}).get("actions") or [])
            problems = contract.validate("panel.set", env.body, actions=actions)
            if problems:
                self.violations.append("panel.set неверен: " + "; ".join(problems))
            else:
                self.panels[conn.module_id or "?"] = env.body
        if env.type == "result":
            fut = conn.pending.get(env.re or "")
            if fut is not None and not fut.done():
                fut.set_result(env)
        elif P.is_request(env.type):
            self._spawn(self._answer(conn, env))

    async def _answer(self, conn: _Conn, env: Envelope) -> None:
        fn = self.handlers.get(env.type)
        if fn is None and contract.is_planned(env.type):        # как ядро (MC-5): описан, но не реализован (обработчик теста главнее)
            reply = P.result_error(env.id, "unavailable", contract.planned_message(env.type))
        elif env.type in contract.CORE_REQUESTS and (problems := contract.validate(env.type, env.body)):
            reply = P.result_error(env.id, "bad_request", "; ".join(problems))      # как ядро (MC-2, MC-7)
        elif fn is None:
            reply = P.result_error(env.id, "unknown", f"FakeCore не знает запрос «{env.type}»")
        else:
            try:
                value = fn(env.body)
                if inspect.isawaitable(value):
                    value = await value
                if value is NO_REPLY:
                    return
                reply = P.result_ok(env.id, value)
            except ToolError as e:
                reply = P.result_error(env.id, e.code, e.message)
            except Exception as e:
                reply = P.result_error(env.id, "internal", str(e) or type(e).__name__)
        await self._send(conn, reply)

    async def _send(self, conn: _Conn, env: Envelope) -> None:
        self.sent.append(env)
        if not conn.ws.closed:
            await conn.ws.send_str(env.dumps())

    def _welcome_body(self, manifest: dict) -> dict:
        shared = manifest.get("shared_settings") or []
        schema = manifest.get("settings_schema") or {}
        module_settings = {k: v["default"] for k, v in schema.items()}
        module_settings.update(self.module_settings or {})
        return {"protocol": P.PROTOCOL, "core_version": self.core_version, "enabled": self.enabled,
                "settings": {k: self.settings[k] for k in shared if k in self.settings},
                "module_settings": module_settings, "profile": self.profile, "battery": self.battery}

    async def _on_hello(self, conn: _Conn, env: Envelope) -> None:
        body = env.body
        reason = None
        if self.reject_next:
            reason = self.reject_next.pop(0)
        else:
            problems = P.validate_hello(body)
            if body.get("protocol") != P.PROTOCOL:
                reason = f"protocol {body.get('protocol')} не поддерживается"
            elif problems:
                reason = "манифест не прошёл проверку: " + "; ".join(problems)
        if reason is not None:
            self.rejected.append(body)
            await self._send(conn, Envelope("reject", {"reason": reason}, re=env.id))
            await conn.ws.close()
            return
        conn.module_id = body["module_id"]
        conn.hello = body
        for old in list(self._conns):
            if old is not conn and old.welcomed and old.module_id == conn.module_id and not old.ws.closed:
                await old.ws.close(code=P.CLOSE_REPLACED, message=b"replaced")     # MOD-13
        conn.welcomed = True
        self.hellos.append(body)
        await self._send(conn, Envelope("welcome", self._welcome_body(body["manifest"]), re=env.id))
        self.conn = conn

    # ------------------------------------------------------------ что видит тест

    def messages_of(self, msg_type: str) -> list[Envelope]:
        """Все сообщения модуля этого типа."""
        return [m for m in self.messages if m.type == msg_type]

    @property
    def events(self) -> list[dict]:
        """Тела всех `event`."""
        return [m.body for m in self.messages_of("event")]

    async def wait_message(self, msg_type: str, predicate: Callable[[dict], bool] | None = None, *,
                           nth: int = 1, timeout: float = 5.0) -> Envelope:
        """Ждать `nth`-е (с единицы) сообщение типа `msg_type`, у которого `predicate(body)` истинно."""
        def matches() -> list[Envelope]:
            return [m for m in self.messages if m.type == msg_type and (predicate is None or predicate(m.body))]

        await self.wait_for(lambda: len(matches()) >= nth, timeout)
        return matches()[nth - 1]

    def install_contract_defaults(self, *names: str) -> None:
        """Ответить на запросы модуля к ядру образцовыми ответами из контракта (`response_valid[0]`), если обработчика нет.
        Без аргументов: на все реализованные запросы (`core.*`, кроме planned). Так FakeCore строится из контракта."""
        for name in names or contract.CORE_REQUESTS:
            msg = contract.get(name)
            if msg is None or contract.is_planned(name) or name in self.handlers or not msg.response_valid:
                continue
            sample = msg.response_valid[0]
            self.handlers[name] = (lambda body, _s=sample: _s)

    def on_request(self, msg_type: str):
        """Обработчик запроса модуля к ядру: `@core.on_request("core.ask") def h(body): return {"answer": True}`.
        ToolError превращается в `ok: false`; NO_REPLY — ядро молчит."""
        def deco(fn: Callable) -> Callable:
            self.handlers[msg_type] = fn
            return fn
        return deco

    # ------------------------------------------------------------ что делает тест от имени ядра

    def _need_conn(self) -> _Conn:
        if self.conn is None:
            raise RuntimeError("модуль не подключён")
        return self.conn

    async def send(self, msg_type: str, body: dict | None = None, *, re: str | None = None) -> Envelope:
        """Послать модулю сообщение без ожидания ответа."""
        env = Envelope(msg_type, dict(body or {}), re=re)
        await self._send(self._need_conn(), env)
        return env

    async def request(self, msg_type: str, body: dict | None = None, *, timeout: float = 5.0) -> Any:
        """Послать запрос модулю и дождаться `result`. ToolError (с кодом) при `ok: false`,
        asyncio.TimeoutError, если модуль не ответил."""
        conn = self._need_conn()
        env = Envelope(msg_type, dict(body or {}))
        fut = asyncio.get_running_loop().create_future()
        conn.pending[env.id] = fut
        try:
            await self._send(conn, env)
            reply = await asyncio.wait_for(fut, timeout)
        finally:
            conn.pending.pop(env.id, None)
        return P.unwrap_result(reply)

    async def workspace(self, action: str, body: dict | None = None, *, legacy: bool = False, timeout: float = 5.0) -> Any:
        """Запрос среды модулю (ТЗ 32): action `enter`, `exit` или `resume`; `legacy=True` шлёт прежнее имя
        (`policy.activate`, `focus.release`, `focus.resume`), которое модуль обрабатывает тем же обработчиком."""
        new = f"workspace.{action}"
        if new not in P.WORKSPACE_ALIASES:
            raise ValueError(f"неизвестное действие среды «{action}»: enter, exit или resume")
        return await self.request(P.WORKSPACE_ALIASES[new] if legacy else new, body, timeout=timeout)

    async def call_tool(self, name: str, args: dict | None = None, *, timeout: float = 5.0) -> Any:
        """`tool.call`; `name` с модулем или без («echo.repeat» и «repeat» равны)."""
        return await self.request("tool.call", {"tool": name.split(".", 1)[-1] if "." in name else name,
                                                "args": args or {}}, timeout=timeout)

    async def check_tool(self, name: str, args: dict | None = None, *, timeout: float = 5.0) -> str | None:
        """`tool.check`: возвращает причину опасности или None."""
        value = await self.request("tool.check", {"tool": name, "args": args or {}}, timeout=timeout)
        return value.get("danger")

    async def panel_action(self, action_id: str, args: dict | None = None, *, timeout: float = 5.0) -> Any:
        """`panel.action`: нажатие кнопки пространства модуля; возвращает `{ok, message?, open_url?}` (TZ 15)."""
        return await self.request("panel.action", {"action_id": action_id, "args": args or {}}, timeout=timeout)

    async def topic(self, topic: str, data: dict | None = None) -> None:
        """Послать тему (`presence`, `settings`, ...)."""
        await self.send("topic", {"topic": topic, "data": data or {}})

    async def set_enabled(self, enabled: bool) -> None:
        """Включить или выключить модуль (тема `module`)."""
        self.enabled = enabled
        await self.topic("module", {"enabled": enabled})

    async def update_settings(self, settings: dict | None = None, module_settings: dict | None = None) -> None:
        """Изменить настройки и разослать тему `settings` (только shared_settings этого модуля)."""
        conn = self._need_conn()
        shared = (conn.hello or {}).get("manifest", {}).get("shared_settings") or []
        self.settings.update(settings or {})
        self.module_settings = {**(self.module_settings or {}), **(module_settings or {})}
        await self.topic("settings", {"settings": {k: self.settings[k] for k in shared if k in self.settings},
                                      "module_settings": self.module_settings})

    async def job_answer(self, job_id: str, question_id: str, answer: Any) -> None:
        await self.send("job.answer", {"job_id": job_id, "question_id": question_id, "answer": answer})

    async def job_cancel(self, job_id: str) -> Any:
        return await self.request("job.cancel", {"job_id": job_id})

    async def job_list(self) -> list[dict]:
        return (await self.request("job.list", {}))["jobs"]

    async def shutdown(self, reason: str = "тест") -> None:
        await self.send("shutdown", {"reason": reason})

    async def drop(self, *, abort: bool = False) -> None:
        """Оборвать соединение с модулем (он должен переподключиться). abort=True: без закрывающего кадра."""
        conn = self._need_conn()
        if abort and conn.transport is not None:
            conn.transport.abort()
        else:
            await conn.ws.close(code=1011, message=b"drop")

    async def kick(self, code: int = P.CLOSE_REPLACED, reason: str = "replaced") -> None:
        """Закрыть соединение с кодом (по умолчанию 4000: модуль вытеснен)."""
        await self._need_conn().ws.close(code=code, message=reason.encode())
