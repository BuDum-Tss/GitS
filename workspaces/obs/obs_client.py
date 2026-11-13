"""Клиент obs-websocket v5 (встроен в OBS 28 и новее): рукопожатие с паролем, запросы, события, переподключение.

Протокол: сервер шлёт Hello (op 0), клиент отвечает Identify (op 1) с ответом на вызов, сервер Identified (op 2); дальше запросы
(op 6) и ответы (op 7), события (op 5). Пароль нигде не печатается."""
import asyncio
import base64
import hashlib
import json
import logging
import uuid
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("obs")

RPC_VERSION = 1
SUBSCRIPTIONS = 1 | 4 | 64          # общие события, сцены, выходы (стрим и запись)
RECONNECT_S = 5.0


class ObsError(Exception):
    """Ошибка OBS: нет связи, отказ запроса, ответ с кодом ошибки."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def auth_string(password: str, salt: str, challenge: str) -> str:
    """Ответ на вызов obs-websocket: base64(sha256(base64(sha256(пароль+соль))+вызов))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    def __init__(self, url: str, password: str = "", on_event: Callable[[str, dict], Awaitable[None]] | None = None):
        self.url, self.password, self.on_event = url, password, on_event
        self.connected = False
        self.last_error = ""
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._session: aiohttp.ClientSession | None = None

    async def run(self) -> None:
        """Держит соединение: подключается, читает сообщения, при обрыве ждёт и пробует снова."""
        while True:
            try:
                await self._connect_and_read()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.info("OBS недоступен: %s", self.last_error)
            self.connected = False
            self._fail_pending("соединение с OBS потеряно")
            await asyncio.sleep(RECONNECT_S)

    async def _connect_and_read(self) -> None:
        self._session = self._session or aiohttp.ClientSession()
        async with self._session.ws_connect(self.url, heartbeat=20) as ws:
            hello = json.loads((await ws.receive(timeout=10)).data)
            if hello.get("op") != 0:
                raise ObsError("OBS не прислал Hello")
            ident: dict = {"rpcVersion": RPC_VERSION, "eventSubscriptions": SUBSCRIPTIONS}
            needs = hello["d"].get("authentication")
            if needs:
                if not self.password:
                    raise ObsError("OBS просит пароль: задай OBS_WEBSOCKET_PASSWORD в .env")
                ident["authentication"] = auth_string(self.password, needs["salt"], needs["challenge"])
            await ws.send_json({"op": 1, "d": ident})
            msg = await ws.receive(timeout=10)
            if msg.type != aiohttp.WSMsgType.TEXT or json.loads(msg.data).get("op") != 2:
                raise ObsError("OBS не принял вход (неверный пароль?)")
            self._ws, self.connected, self.last_error = ws, True, ""
            log.info("OBS подключён")
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                await self._handle(json.loads(msg.data))
        self._ws = None

    async def _handle(self, data: dict) -> None:
        op, d = data.get("op"), data.get("d") or {}
        if op == 7:
            fut = self._pending.pop(d.get("requestId", ""), None)
            if fut and not fut.done():
                status = d.get("requestStatus") or {}
                if status.get("result"):
                    fut.set_result(d.get("responseData") or {})
                else:
                    fut.set_exception(ObsError(status.get("comment") or "запрос отклонён OBS", status.get("code")))
        elif op == 5 and self.on_event:
            try:
                await self.on_event(d.get("eventType", ""), d.get("eventData") or {})
            except Exception as e:                                   # обработчик события не должен рвать чтение
                log.warning("обработчик события OBS: %s", e)

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ObsError(reason))
        self._pending.clear()

    async def request(self, request_type: str, data: dict | None = None, timeout: float = 10.0) -> dict:
        if not self.connected or self._ws is None:
            raise ObsError(self.last_error or "OBS не подключён: запусти OBS и включи WebSocket-сервер")
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send_json({"op": 6, "d": {"requestType": request_type, "requestId": rid, **({"requestData": data} if data else {})}})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self._pending.pop(rid, None)
            raise ObsError(f"OBS не ответил на {request_type}") from e

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
