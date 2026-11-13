"""Интерфейс управления workspace: аренда общего окружения (контракт 1.11, фаза 1 переименования).

Workspace с `ownership: shared` (montage, rimworld, minecraft, desktop) занимает один хозяин за раз. Хозяин (ядро персонажа)
берёт окружение в аренду по HTTP, а workspace подключается к его ядру по WebSocket:

    # сторона workspace
    async def on_acquire(lease):                    # lease.core_url, lease.token, lease.character, lease.purpose
        client.connect_to(lease.core_url, lease.token)
    server = ControlServer(id="rimworld", manifest=MANIFEST, on_acquire=on_acquire, on_release=on_release)   # токен: GITS_CONTROL_TOKEN
    await server.start("127.0.0.1", 8790)
    # WebSocket участника оборвался: аренда отпускается (client.on_disconnect -> server.connection_lost(lease.lease_id))

    # сторона ядра
    leases = LeaseClient("http://127.0.0.1:8790", token)
    about = await leases.info()                     # для чего окружение и когда в него переходить (до аренды)
    lease = await leases.acquire(character="glados", core_url=core_url, token=core_token, purpose="play")
    await leases.release(lease["lease_id"])

Описание форматов: `agent/interfaces/workspace/gits_sdk/contract/control.py` и раздел «Интерфейс управления» в `docs/MODULE_CONTRACT.md`.
У аренды нет срока и продления: она держится до `release` или до обрыва WebSocket участника (`connection_lost`). Если после `release`
у workspace есть незаконченные задачи, аренда остаётся в `releasing` (`on_release` вернул `False`) до `finish_release`.
Для участников без аренды (`ownership: owned`, service) сервер отвечает на `GET /status` (state `free` или `offline`, счётчик
`active`) и `GET /info`, а `acquire` и `release` отвечают 404. Токены (управления и ядра хозяина) в журнал не попадают.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp
from aiohttp import web

from . import protocol as P
from .contract import control as C
from .contract.spec import check_fields

log = logging.getLogger("gits_sdk.control")

CLIENT_TIMEOUT_S = 10.0         # тайм-аут одного запроса клиента аренды


# ---------------------------------------------------------------- аренда

@dataclass
class Lease:
    """Одна аренда. Токен ядра хозяина (`token`) хранится только здесь, в `holder` и журнал не попадает."""

    lease_id: str
    character: str
    core_url: str
    token: str
    purpose: str
    since: float
    releasing: bool = False        # агент вышел, но у workspace ещё есть работа: аренда держится до `finish_release`
    jobs: int = 0                  # сколько активных задач осталось (при releasing)
    why: str = ""                  # причина вызова `on_release`: release, finished (работа кончилась), disconnect, stop, replaced

    def holder(self) -> dict:
        body = {"character": self.character, "core_url": self.core_url, "since": self.since, "purpose": self.purpose}
        if self.releasing:
            body.update(releasing=True, jobs=self.jobs)
        return body

    def __repr__(self) -> str:
        return f"Lease({self.lease_id}, {self.character!r}, {self.purpose!r})"


class ControlError(Exception):
    """Ошибка настройки сервера управления (например, нет токена)."""


Hook = Callable[[Lease], Awaitable[Any] | Any]


def _json(status: int, body: dict) -> web.Response:
    return web.json_response(body, status=status)


def _err(status: int, error: str, message: str = "", **extra: Any) -> web.Response:
    body: dict[str, Any] = {"ok": False, "error": error}
    if message:
        body["message"] = message
    body.update(extra)
    return _json(status, body)


class ControlServer:
    """Сервер управления (aiohttp, HTTP). Один холдер за раз; срока и продления нет.

    id, role, ownership: что сообщает `GET /status`. Аренда включена только при `ownership == "shared"` (и роли workspace);
    иначе доступен лишь `GET /status`, остальное отвечает 404. `on_acquire(lease)` и `on_release(lease)` (функции или
    корутины) подключают и отключают workspace от ядра хозяина; аренда кончается по `POST /release` или по
    `connection_lost()` (workspace зовёт его, когда оборвалась связь с ядром хозяина). Если при `release` у workspace есть незаконченная
    работа, `on_release` возвращает `False`: аренда переходит в `releasing` (`holder.releasing`, `holder.jobs` из `jobs()`), другой
    хозяин получает 409, тот же `character` возвращает аренду запросом `acquire`, а когда работа кончилась, workspace зовёт
    `finish_release(lease_id)`. `manifest` даёт `GET /info`.
    `available()` сообщает, доступно ли окружение (иначе state `offline`, acquire отвечает 503). `active()` даёт счётчик
    подключений для участников без аренды. `clock` подменяется в тестах. Без токена управления (параметр или `GITS_CONTROL_TOKEN`) сервер не создаётся: `ControlError`."""

    def __init__(self, *, id: str, token: str | None = None, role: str = P.ROLE_WORKSPACE,
                 ownership: str | None = P.OWNERSHIP_SHARED, on_acquire: Hook | None = None, on_release: Hook | None = None,
                 available: Callable[[], bool] | None = None, active: Callable[[], int] | None = None,
                 manifest: dict | None = None, jobs: Callable[[], int] | None = None, clock: Callable[[], float] = time.time):
        token = token or os.environ.get(P.ENV_CONTROL_TOKEN) or ""
        if not token:
            raise ControlError(f"нет токена управления: задайте параметр token или переменную {P.ENV_CONTROL_TOKEN}")
        if role not in P.ROLES:
            raise ControlError(f"role: одно из {list(P.ROLES)}")
        if role == P.ROLE_SERVICE:
            ownership = None
        elif ownership not in P.OWNERSHIPS:
            raise ControlError(f"ownership: одно из {list(P.OWNERSHIPS)}")
        self.id = id
        self.role = role
        self.ownership = ownership
        self.manifest = manifest or {}
        self._jobs = jobs
        self._token = token
        self._on_acquire = on_acquire
        self._on_release = on_release
        self._available = available
        self._active = active
        self._clock = clock
        self.lease: Lease | None = None
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._port = 0

    # ------------------------------------------------------------ сервер

    @property
    def leasing(self) -> bool:
        """Есть ли аренда: только у workspace с ownership shared."""
        return self.role == P.ROLE_WORKSPACE and self.ownership == P.OWNERSHIP_SHARED

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def make_app(self) -> web.Application:
        """aiohttp-приложение (для встраивания в чужой сервер или тестов через TestClient)."""
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/status", self._status)
        app.router.add_get("/info", self._info)
        app.router.add_post("/acquire", self._acquire)
        app.router.add_post("/release", self._release)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._runner = web.AppRunner(self.make_app(), shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self._port = self._runner.addresses[0][1]
        log.info("интерфейс управления «%s» слушает %s:%d", self.id, host, self._port)

    async def stop(self) -> None:
        """Остановить сервер; действующая аренда закрывается (вызывается `on_release`)."""
        async with self._lock:
            if self.lease is not None:
                await self._close_lease("stop")
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # ------------------------------------------------------------ конец аренды

    async def connection_lost(self, lease_id: str | None = None) -> bool:
        """Связь workspace с ядром хозяина оборвалась: аренда отпускается (как `release`). `lease_id`: закрыть только эту аренду
        (опоздавший сигнал о старой аренде новую не трогает). True, если аренда была закрыта."""
        async with self._lock:
            if self.lease is None or (lease_id is not None and self.lease.lease_id != lease_id):
                return False
            log.info("связь «%s» с ядром хозяина %s оборвалась, аренда отпущена", self.id, self.lease.character)
            await self._close_lease("disconnect")
            return True

    async def _close_lease(self, why: str, *, may_wait: bool = False) -> Lease | None:
        """Забыть хозяина и вызвать `on_release`. Состояние сбрасывается до вызова: сбой обработчика аренду не удерживает.
        `may_wait`: обработчик может ответить `False` (есть работа): тогда аренда остаётся в `releasing` и возвращается None."""
        lease = self.lease
        if lease is None:
            return None
        res: Any = None
        lease.why = why
        if self._on_release is not None:
            try:
                res = self._on_release(lease)
                if inspect.isawaitable(res):
                    res = await res
            except Exception:
                log.exception("on_release для «%s» упал (%s)", self.id, why)
                res = None
        if may_wait and res is False:
            lease.releasing = True
            log.info("аренда «%s» (хозяин %s) ждёт конца задач", self.id, lease.character)
            return None
        self.lease = None
        return lease

    async def finish_release(self, lease_id: str | None = None) -> bool:
        """Работа кончилась: довести отложенное освобождение (`on_release` вызывается снова). True, если аренда закрыта."""
        async with self._lock:
            cur = self.lease
            if cur is None or not cur.releasing or (lease_id is not None and cur.lease_id != lease_id):
                return False
            closed = await self._close_lease("finished", may_wait=True)
            if closed is not None:
                log.info("аренда «%s» (хозяин %s) завершена после задач", self.id, closed.character)
            return closed is not None

    # ------------------------------------------------------------ HTTP

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        given = header[7:] if header[:7].lower() == "bearer " else ""
        if not given or not hmac.compare_digest(given.encode(), self._token.encode()):
            return _err(401, "unauthorized")
        return await handler(request)

    def _holder(self) -> dict | None:
        if self.lease is None:
            return None
        if self.lease.releasing and self._jobs is not None:
            self.lease.jobs = int(self._jobs())
        return self.lease.holder()

    def _state(self) -> str:
        if self.lease is not None:
            return C.STATE_BUSY
        if self._available is not None and not self._available():
            return C.STATE_OFFLINE
        return C.STATE_FREE

    def status(self) -> dict:
        """Тело `GET /status`."""
        body: dict[str, Any] = {"id": self.id, "role": self.role}
        if self.ownership is not None:
            body["ownership"] = self.ownership
        active = int(self._active()) if self._active is not None else (1 if self.lease is not None else 0)
        body.update(state=self._state(), holder=self._holder(), active=active)
        return body

    def info(self) -> dict:
        """Тело `GET /info`: сведения о workspace из манифеста и состояние аренды."""
        m = self.manifest
        ws = m.get("workspace") if isinstance(m.get("workspace"), dict) else {}
        body: dict[str, Any] = {"id": self.id, "role": self.role, "title": ws.get("title") or m.get("title") or self.id}
        body["purpose"] = ws.get("purpose") or m.get("description") or ""
        body["when_to_use"] = ws.get("when_to_use") or ""
        body["done_when"] = ws.get("done_when") or ""
        body["keywords"] = list(ws.get("keywords") or [])
        body["entry"] = list(ws.get("entry") or [])
        body["interruptible"] = bool(ws.get("interruptible", True))
        body["safe_points"] = list(ws.get("safe_points") or [])
        body["tools"] = [{"name": t.get("name"), "description": t.get("description", "")} for t in m.get("tools") or []
                         if isinstance(t, dict)]
        body["state"] = self._state()
        body["holder"] = self._holder()
        return body

    async def _status(self, request: web.Request) -> web.Response:
        return _json(200, self.status())

    async def _info(self, request: web.Request) -> web.Response:
        return _json(200, self.info())

    async def _body(self, request: web.Request, endpoint: C.Endpoint) -> tuple[dict | None, web.Response | None]:
        try:
            body = await request.json()
        except Exception:
            return None, _err(400, "bad_request", "тело должно быть JSON-объектом")
        problems = check_fields(endpoint.request, body)
        if problems:
            return None, _err(400, "bad_request", "; ".join(problems))
        return body, None

    async def _acquire(self, request: web.Request) -> web.Response:
        if not self.leasing:
            return _err(404, "no_lease", "у этого участника нет аренды")
        body, bad = await self._body(request, C.ACQUIRE)
        if bad is not None:
            return bad
        if not body["core_url"].startswith(("ws://", "wss://")):
            return _err(400, "bad_request", "core_url: адрес ws:// или wss://")
        purpose = body.get("purpose") or ""
        async with self._lock:
            now = self._clock()
            cur = self.lease
            if cur is not None:
                if cur.character != body["character"] or (cur.purpose != purpose and not cur.releasing):
                    return _json(409, {"ok": False, "state": C.STATE_BUSY, "holder": self._holder()})
                if cur.core_url == body["core_url"] and cur.token == body["token"]:     # повторный запрос: та же аренда
                    if cur.releasing:                                                   # хозяин вернулся: выход отменён
                        cur.releasing, cur.jobs, cur.purpose = False, 0, purpose
                    return _json(200, {"ok": True, "lease_id": cur.lease_id})
                await self._close_lease("replaced")                                     # ядро хозяина сменилось
            elif self._available is not None and not self._available():
                return _json(503, {"ok": False, "state": C.STATE_OFFLINE})
            lease = Lease("l-" + secrets.token_hex(6), body["character"], body["core_url"], body["token"], purpose,
                          since=now)
            if self._on_acquire is not None:
                try:
                    res = self._on_acquire(lease)
                    if inspect.isawaitable(res):
                        await res
                except Exception as e:
                    log.warning("on_acquire для «%s» не удался: %s", self.id, type(e).__name__)
                    return _err(502, "acquire_failed", str(e) or type(e).__name__)
            self.lease = lease
            log.info("аренда «%s» выдана: %s (%s)", self.id, lease.character, lease.purpose or "-")
            return _json(200, {"ok": True, "lease_id": lease.lease_id})

    async def _release(self, request: web.Request) -> web.Response:
        if not self.leasing:
            return _err(404, "no_lease", "у этого участника нет аренды")
        body, bad = await self._body(request, C.RELEASE)
        if bad is not None:
            return bad
        async with self._lock:
            cur = self.lease
            if cur is None or cur.lease_id != body["lease_id"]:
                return _json(200, {"ok": True, "released": False})
            if await self._close_lease("release", may_wait=True) is None:
                return _json(200, {"ok": True, "released": False, "pending": "jobs"})
            return _json(200, {"ok": True, "released": True})


# ---------------------------------------------------------------- клиентская часть для ядра

class LeaseError(Exception):
    """Ошибка запроса аренды. `status`: HTTP-код (0: нет связи), `error`: код из тела, `state` и `holder` при занятости."""

    def __init__(self, message: str, *, status: int = 0, error: str = "", state: str | None = None,
                 holder: dict | None = None):
        super().__init__(message)
        self.status = status
        self.error = error
        self.state = state
        self.holder = holder


class LeaseUnavailable(LeaseError):
    """Нет связи с интерфейсом управления (отказ в соединении, тайм-аут)."""


class LeaseBusy(LeaseError):
    """409: окружение занято другим хозяином (`holder`)."""


class LeaseUnauthorized(LeaseError):
    """401: неверный токен управления."""


class LeaseClient:
    """Клиент аренды для ядра: `status`, `info`, `acquire`, `release`. Каждый вызов с тайм-аутом; ошибки: `LeaseError`
    и её потомки (`LeaseUnavailable`, `LeaseBusy`, `LeaseUnauthorized`). Токен управления в сообщения об ошибках не попадает."""

    def __init__(self, base_url: str, token: str, *, timeout: float = CLIENT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    async def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        url = self.base_url + path
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.request(method, url, json=body, headers={"Authorization": f"Bearer {self._token}"}) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                    status = resp.status
        except asyncio.TimeoutError as e:
            raise LeaseUnavailable(f"интерфейс управления {self.base_url} не ответил за {self.timeout:g} с") from e
        except (aiohttp.ClientError, OSError) as e:
            raise LeaseUnavailable(f"интерфейс управления {self.base_url} недоступен: {type(e).__name__}") from e
        if not isinstance(data, dict):
            data = {}
        if status == 200:
            return data
        error, message = str(data.get("error") or ""), str(data.get("message") or "")
        if status == 401:
            raise LeaseUnauthorized("токен управления отвергнут", status=status, error=error or "unauthorized")
        if status == 409:
            holder = data.get("holder") if isinstance(data.get("holder"), dict) else None
            who = f" (хозяин: {holder.get('character')})" if holder else ""
            raise LeaseBusy("окружение занято" + who, status=status, error="busy", state=data.get("state"), holder=holder)
        if status == 503 and data.get("state") == C.STATE_OFFLINE:
            raise LeaseError("окружение недоступно (offline)", status=status, error="offline", state=C.STATE_OFFLINE)
        raise LeaseError(f"{method} {path}: HTTP {status}" + (f", {error}" if error else "") + (f": {message}" if message else ""),
                         status=status, error=error, state=data.get("state"))

    async def status(self) -> dict:
        return await self._call("GET", "/status")

    async def info(self) -> dict:
        """Сведения о workspace (`GET /info`): для чего он и когда в него переходить, состояние аренды."""
        return await self._call("GET", "/info")

    async def acquire(self, *, character: str, core_url: str, token: str, purpose: str = "") -> dict:
        """Взять в аренду: `{"ok", "lease_id"}`. `LeaseBusy`, если занято другим."""
        body: dict[str, Any] = {"character": character, "core_url": core_url, "token": token, "purpose": purpose}
        return await self._call("POST", "/acquire", body)

    async def release(self, lease_id: str) -> dict:
        return await self._call("POST", "/release", {"lease_id": lease_id})
