"""Интерфейс управления workspace: аренда общего окружения (контракт 1.11, фаза 1 переименования).

Workspace с `ownership: shared` (montage, rimworld, minecraft, desktop) занимает один хозяин за раз. Хозяин (ядро персонажа)
берёт окружение в аренду по HTTP, а workspace подключается к его ядру по WebSocket:

    # сторона workspace
    async def on_acquire(lease):                    # lease.core_url, lease.token, lease.character, lease.purpose
        client.connect_to(lease.core_url, lease.token)
    server = ControlServer(id="rimworld", on_acquire=on_acquire, on_release=on_release)   # токен: GITS_CONTROL_TOKEN
    await server.start("127.0.0.1", 8790)

    # сторона ядра
    leases = LeaseClient("http://127.0.0.1:8790", token)
    lease = await leases.acquire(character="glados", core_url=core_url, token=core_token, purpose="play")
    await leases.renew(lease["lease_id"]); await leases.release(lease["lease_id"])

Описание форматов: `sdk/gits_sdk/contract/control.py` и раздел «Интерфейс управления» в `docs/MODULE_CONTRACT.md`.
Для участников без аренды (`ownership: owned`, service) сервер отвечает на `GET /status` (state `free` или `offline`, счётчик
`active`), а `acquire`, `renew` и `release` отвечают 404. Токены (управления и ядра хозяина) в журнал не попадают.
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
WATCH_INTERVAL_S = 1.0          # как часто сервер проверяет истечение срока (кроме проверки при каждом запросе)


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
    expires: float
    ttl_s: float

    def holder(self) -> dict:
        return {"character": self.character, "core_url": self.core_url, "since": self.since,
                "purpose": self.purpose, "expires": self.expires}

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
    """Сервер управления (aiohttp, HTTP). Один холдер за раз, срок аренды истекает сам.

    id, role, ownership: что сообщает `GET /status`. Аренда включена только при `ownership == "shared"` (и роли workspace);
    иначе доступен лишь `GET /status`, остальное отвечает 404. `on_acquire(lease)` и `on_release(lease)` (функции или
    корутины) подключают и отключают workspace от ядра хозяина. `available()` сообщает, доступно ли окружение (иначе state
    `offline`, acquire отвечает 503). `active()` даёт счётчик подключений для участников без аренды. `clock` подменяется в
    тестах. Без токена управления (параметр или `GITS_CONTROL_TOKEN`) сервер не создаётся: `ControlError`."""

    def __init__(self, *, id: str, token: str | None = None, role: str = P.ROLE_WORKSPACE,
                 ownership: str | None = P.OWNERSHIP_SHARED, on_acquire: Hook | None = None, on_release: Hook | None = None,
                 available: Callable[[], bool] | None = None, active: Callable[[], int] | None = None,
                 default_ttl_s: float = C.LEASE_TTL_DEFAULT_S, max_ttl_s: float = C.LEASE_TTL_MAX_S,
                 clock: Callable[[], float] = time.time):
        token = token or os.environ.get(P.ENV_CONTROL_TOKEN) or ""
        if not token:
            raise ControlError(f"нет токена управления: задайте параметр token или переменную {P.ENV_CONTROL_TOKEN}")
        if role not in P.ROLES:
            raise ControlError(f"role: одно из {list(P.ROLES)}")
        if role == P.ROLE_SERVICE:
            ownership = None
        elif ownership not in P.OWNERSHIPS:
            raise ControlError(f"ownership: одно из {list(P.OWNERSHIPS)}")
        if not 0 < default_ttl_s <= max_ttl_s:
            raise ControlError("default_ttl_s должен быть больше нуля и не больше max_ttl_s")
        self.id = id
        self.role = role
        self.ownership = ownership
        self.default_ttl_s = float(default_ttl_s)
        self.max_ttl_s = float(max_ttl_s)
        self._token = token
        self._on_acquire = on_acquire
        self._on_release = on_release
        self._available = available
        self._active = active
        self._clock = clock
        self.lease: Lease | None = None
        self._lock = asyncio.Lock()
        self._runner: web.AppRunner | None = None
        self._watcher: asyncio.Task | None = None
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
        app.router.add_post("/acquire", self._acquire)
        app.router.add_post("/renew", self._renew)
        app.router.add_post("/release", self._release)
        return app

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._runner = web.AppRunner(self.make_app(), shutdown_timeout=1.0)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host, port)
        await site.start()
        self._port = self._runner.addresses[0][1]
        if self.leasing:
            self._watcher = asyncio.create_task(self._watch())
        log.info("интерфейс управления «%s» слушает %s:%d", self.id, host, self._port)

    async def stop(self) -> None:
        """Остановить сервер; действующая аренда закрывается (вызывается `on_release`)."""
        if self._watcher is not None:
            self._watcher.cancel()
            await asyncio.gather(self._watcher, return_exceptions=True)
            self._watcher = None
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

    # ------------------------------------------------------------ срок аренды

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            try:
                await self.expire_if_due()
            except Exception:
                log.exception("сбой проверки срока аренды")

    async def expire_if_due(self) -> bool:
        """Закрыть аренду, срок которой истёк (как release). True, если закрыта."""
        async with self._lock:
            return await self._expire_locked()

    async def _expire_locked(self) -> bool:
        lease = self.lease
        if lease is not None and self._clock() >= lease.expires:
            log.info("аренда «%s» (хозяин %s) истекла", self.id, lease.character)
            await self._close_lease("expired")
            return True
        return False

    async def _close_lease(self, why: str) -> Lease | None:
        """Забыть хозяина и вызвать `on_release`. Состояние сбрасывается до вызова: сбой обработчика аренду не удерживает."""
        lease, self.lease = self.lease, None
        if lease is not None and self._on_release is not None:
            try:
                res = self._on_release(lease)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                log.exception("on_release для «%s» упал (%s)", self.id, why)
        return lease

    # ------------------------------------------------------------ HTTP

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        given = header[7:] if header[:7].lower() == "bearer " else ""
        if not given or not hmac.compare_digest(given.encode(), self._token.encode()):
            return _err(401, "unauthorized")
        return await handler(request)

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
        body.update(state=self._state(), holder=self.lease.holder() if self.lease else None,
                    ttl_s=self.default_ttl_s, active=active)
        return body

    async def _status(self, request: web.Request) -> web.Response:
        if self.leasing:
            async with self._lock:
                await self._expire_locked()
        return _json(200, self.status())

    async def _body(self, request: web.Request, endpoint: C.Endpoint) -> tuple[dict | None, web.Response | None]:
        try:
            body = await request.json()
        except Exception:
            return None, _err(400, "bad_request", "тело должно быть JSON-объектом")
        if isinstance(body, dict) and isinstance(body.get("ttl_s"), (int, float)) and not isinstance(body["ttl_s"], bool):
            body = {**body, "ttl_s": min(body["ttl_s"], self.max_ttl_s)}              # слишком большой срок обрезается, не отвергается
        problems = check_fields(endpoint.request, body)
        if problems:
            return None, _err(400, "bad_request", "; ".join(problems))
        return body, None

    def _ttl(self, body: dict) -> float:
        ttl = body.get("ttl_s")
        return self.default_ttl_s if ttl is None else min(float(ttl), self.max_ttl_s)

    async def _acquire(self, request: web.Request) -> web.Response:
        if not self.leasing:
            return _err(404, "no_lease", "у этого участника нет аренды")
        body, bad = await self._body(request, C.ACQUIRE)
        if bad is not None:
            return bad
        if not body["core_url"].startswith(("ws://", "wss://")):
            return _err(400, "bad_request", "core_url: адрес ws:// или wss://")
        purpose = body.get("purpose") or ""
        ttl = self._ttl(body)
        async with self._lock:
            await self._expire_locked()
            now = self._clock()
            cur = self.lease
            if cur is not None:
                if cur.character != body["character"] or cur.purpose != purpose:
                    return _json(409, {"ok": False, "state": C.STATE_BUSY, "holder": cur.holder()})
                if cur.core_url == body["core_url"] and cur.token == body["token"]:     # идемпотентное продление
                    cur.expires, cur.ttl_s = now + ttl, ttl
                    return _json(200, {"ok": True, "lease_id": cur.lease_id, "expires": cur.expires, "ttl_s": ttl})
                await self._close_lease("replaced")                                     # ядро хозяина сменилось
            elif self._available is not None and not self._available():
                return _json(503, {"ok": False, "state": C.STATE_OFFLINE})
            lease = Lease("l-" + secrets.token_hex(6), body["character"], body["core_url"], body["token"], purpose,
                          since=now, expires=now + ttl, ttl_s=ttl)
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
            return _json(200, {"ok": True, "lease_id": lease.lease_id, "expires": lease.expires, "ttl_s": ttl})

    async def _renew(self, request: web.Request) -> web.Response:
        if not self.leasing:
            return _err(404, "no_lease", "у этого участника нет аренды")
        body, bad = await self._body(request, C.RENEW)
        if bad is not None:
            return bad
        async with self._lock:
            await self._expire_locked()
            cur = self.lease
            if cur is None or cur.lease_id != body["lease_id"]:
                return _err(404, "unknown_lease", "аренды нет, она истекла или закрыта")
            ttl = self._ttl(body)
            cur.expires, cur.ttl_s = self._clock() + ttl, ttl
            return _json(200, {"ok": True, "lease_id": cur.lease_id, "expires": cur.expires, "ttl_s": ttl})

    async def _release(self, request: web.Request) -> web.Response:
        if not self.leasing:
            return _err(404, "no_lease", "у этого участника нет аренды")
        body, bad = await self._body(request, C.RELEASE)
        if bad is not None:
            return bad
        async with self._lock:
            await self._expire_locked()
            cur = self.lease
            if cur is None or cur.lease_id != body["lease_id"]:
                return _json(200, {"ok": True, "released": False})
            await self._close_lease("release")
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
    """Клиент аренды для ядра: `status`, `acquire`, `renew`, `release`. Каждый вызов с тайм-аутом; ошибки: `LeaseError`
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

    async def acquire(self, *, character: str, core_url: str, token: str, purpose: str = "",
                      ttl_s: float | None = None) -> dict:
        """Взять в аренду: `{"ok", "lease_id", "expires", "ttl_s"}`. `LeaseBusy`, если занято другим."""
        body: dict[str, Any] = {"character": character, "core_url": core_url, "token": token, "purpose": purpose}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        return await self._call("POST", "/acquire", body)

    async def renew(self, lease_id: str, ttl_s: float | None = None) -> dict:
        body: dict[str, Any] = {"lease_id": lease_id}
        if ttl_s is not None:
            body["ttl_s"] = ttl_s
        return await self._call("POST", "/renew", body)

    async def release(self, lease_id: str) -> dict:
        return await self._call("POST", "/release", {"lease_id": lease_id})
