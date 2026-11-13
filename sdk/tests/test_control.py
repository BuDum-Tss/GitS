"""Интерфейс аренды workspace (контракт 1.11): сервер управления и клиент ядра. Только петля 127.0.0.1."""
import asyncio

import pytest

from gits_sdk import protocol as P
from gits_sdk.contract import control as C
from gits_sdk.control import (ControlError, ControlServer, LeaseBusy, LeaseClient, LeaseError, LeaseUnauthorized,
                              LeaseUnavailable)

TOKEN = "control-secret"
CORE = "ws://127.0.0.1:8766/module/ws"


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Hooks:
    def __init__(self):
        self.acquired: list = []
        self.released: list = []
        self.fail = False

    async def on_acquire(self, lease):
        if self.fail:
            raise RuntimeError("ядро хозяина недоступно")
        self.acquired.append(lease)

    def on_release(self, lease):                       # обычная функция: тоже допустима
        self.released.append(lease)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hooks():
    return Hooks()


@pytest.fixture
async def server(clock, hooks):
    async with ControlServer(id="rimworld", token=TOKEN, ownership="shared", on_acquire=hooks.on_acquire,
                             on_release=hooks.on_release, clock=clock) as s:
        yield s


@pytest.fixture
def lc(server):
    return LeaseClient(server.url, TOKEN, timeout=5)


async def acquire(lc, character="glados", purpose="play", **kw):
    return await lc.acquire(character=character, core_url=CORE, token="core-token", purpose=purpose, **kw)


# ---------------------------------------------------------------- основной путь

async def test_free_acquire_busy_renew_release(lc, server, hooks, clock):
    st = await lc.status()
    assert st == {"id": "rimworld", "role": "workspace", "ownership": "shared", "state": "free", "holder": None,
                  "ttl_s": 300.0, "active": 0}
    got = await acquire(lc)
    assert got["ok"] and got["lease_id"].startswith("l-") and got["ttl_s"] == 300
    assert got["expires"] == clock.now + 300
    assert len(hooks.acquired) == 1
    lease = hooks.acquired[0]
    assert (lease.character, lease.core_url, lease.token, lease.purpose) == ("glados", CORE, "core-token", "play")

    st = await lc.status()
    assert st["state"] == "busy" and st["active"] == 1
    assert st["holder"] == {"character": "glados", "core_url": CORE, "since": clock.now, "purpose": "play",
                            "expires": clock.now + 300}
    assert "token" not in str(st)                                    # токен ядра хозяина наружу не отдаётся

    with pytest.raises(LeaseBusy) as busy:                          # другой персонаж
        await acquire(lc, character="other")
    assert busy.value.status == 409 and busy.value.holder["character"] == "glados" and "token" not in busy.value.holder
    with pytest.raises(LeaseBusy):                                  # тот же персонаж, но другая цель
        await acquire(lc, purpose="montage")
    assert len(hooks.acquired) == 1

    clock.now += 100
    renewed = await lc.renew(got["lease_id"], ttl_s=600)
    assert renewed["ok"] and renewed["expires"] == clock.now + 600 and renewed["ttl_s"] == 600
    assert (await lc.status())["holder"]["expires"] == clock.now + 600

    assert await lc.release(got["lease_id"]) == {"ok": True, "released": True}
    assert hooks.released == [lease]
    assert (await lc.status())["state"] == "free"
    assert await lc.release(got["lease_id"]) == {"ok": True, "released": False}     # повторный release безвреден
    assert len(hooks.released) == 1
    again = await acquire(lc, character="other")                    # после release доступно другому
    assert again["lease_id"] != got["lease_id"]


async def test_expiry_acts_like_release(lc, server, hooks, clock):
    got = await acquire(lc, ttl_s=60)
    assert got["ttl_s"] == 60
    clock.now += 59
    assert (await lc.status())["state"] == "busy"
    clock.now += 2
    assert (await lc.status())["state"] == "free"
    assert len(hooks.released) == 1 and hooks.released[0].lease_id == got["lease_id"]
    with pytest.raises(LeaseError) as e:                            # продлить истёкшую нельзя
        await lc.renew(got["lease_id"])
    assert e.value.status == 404 and e.value.error == "unknown_lease"
    await acquire(lc, character="other")                            # место свободно


async def test_expired_lease_does_not_block_acquire(lc, hooks, clock):
    await acquire(lc, ttl_s=10)
    clock.now += 11
    other = await acquire(lc, character="other")                    # без обращения к status: истечение при acquire
    assert other["ok"] and len(hooks.released) == 1 and len(hooks.acquired) == 2


async def test_background_watcher_expires_lease(hooks, monkeypatch):
    monkeypatch.setattr("gits_sdk.control.WATCH_INTERVAL_S", 0.02)
    async with ControlServer(id="rimworld", token=TOKEN, on_release=hooks.on_release, default_ttl_s=1) as s:
        lc = LeaseClient(s.url, TOKEN)
        await acquire(lc, ttl_s=1)
        for _ in range(200):
            if hooks.released:
                break
            await asyncio.sleep(0.02)
        assert len(hooks.released) == 1 and s.lease is None


async def test_idempotent_acquire_same_character_and_purpose(lc, hooks, clock):
    first = await acquire(lc)
    clock.now += 100
    second = await acquire(lc)                                      # то же самое: продление
    assert second["lease_id"] == first["lease_id"] and second["expires"] == clock.now + 300
    assert len(hooks.acquired) == 1 and hooks.released == []


async def test_acquire_with_new_core_replaces_lease_of_same_holder(lc, hooks):
    first = await acquire(lc)
    second = await lc.acquire(character="glados", core_url="ws://127.0.0.1:9999/module/ws", token="new-token", purpose="play")
    assert second["lease_id"] != first["lease_id"]
    assert len(hooks.released) == 1 and len(hooks.acquired) == 2 and hooks.acquired[1].token == "new-token"


async def test_ttl_default_and_clamp(lc, server, clock):
    assert (await acquire(lc))["ttl_s"] == 300
    got = await acquire(lc, ttl_s=99999)                            # то же аренда, продление с обрезкой до 3600
    assert got["ttl_s"] == C.LEASE_TTL_MAX_S == 3600 and got["expires"] == clock.now + 3600
    assert server.default_ttl_s == C.LEASE_TTL_DEFAULT_S


async def test_on_acquire_failure_gives_502_and_no_lease(lc, hooks):
    hooks.fail = True
    with pytest.raises(LeaseError) as e:
        await acquire(lc)
    assert e.value.status == 502 and e.value.error == "acquire_failed"
    assert (await lc.status())["state"] == "free"
    hooks.fail = False
    assert (await acquire(lc))["ok"]


async def test_offline_workspace_refuses_acquire(clock):
    up = {"v": False}
    async with ControlServer(id="rimworld", token=TOKEN, clock=clock, available=lambda: up["v"]) as s:
        lc = LeaseClient(s.url, TOKEN)
        assert (await lc.status())["state"] == "offline"
        with pytest.raises(LeaseError) as e:
            await acquire(lc)
        assert e.value.status == 503 and e.value.state == "offline"
        up["v"] = True
        assert (await lc.status())["state"] == "free"
        assert (await acquire(lc))["ok"]


async def test_stop_releases_current_lease(clock, hooks):
    async with ControlServer(id="rimworld", token=TOKEN, clock=clock, on_release=hooks.on_release) as s:
        await acquire(LeaseClient(s.url, TOKEN))
    assert len(hooks.released) == 1


# ---------------------------------------------------------------- авторизация и проверка тела

async def test_wrong_or_missing_token_gets_401(server, hooks):
    bad = LeaseClient(server.url, "чужой")
    for call in (bad.status, lambda: acquire(bad), lambda: bad.renew("x"), lambda: bad.release("x")):
        with pytest.raises(LeaseUnauthorized) as e:
            await call()
        assert e.value.status == 401 and "чужой" not in str(e.value)
    assert hooks.acquired == []
    import aiohttp
    async with aiohttp.ClientSession() as s:                         # совсем без заголовка
        async with s.get(server.url + "/status") as r:
            assert r.status == 401


async def test_bad_bodies_get_400(server, lc):
    import aiohttp
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {TOKEN}"}) as s:
        for path, body in (("/acquire", {}), ("/acquire", {"character": "a", "core_url": "http://x", "token": "t"}),
                           ("/acquire", {"character": "a", "core_url": CORE, "token": "t", "ttl_s": 0}),
                           ("/acquire", {"character": "", "core_url": CORE, "token": "t"}),
                           ("/acquire", [1]), ("/renew", {}), ("/release", {"lease_id": 5})):
            async with s.post(server.url + path, json=body) as r:
                assert r.status == 400, (path, body)
                assert (await r.json())["error"] == "bad_request"
        async with s.post(server.url + "/acquire", data=b"not json") as r:
            assert r.status == 400
    assert (await lc.status())["state"] == "free"


async def test_renew_unknown_lease_404(lc):
    await acquire(lc)
    with pytest.raises(LeaseError) as e:
        await lc.renew("l-deadbeef0000")
    assert e.value.status == 404


# ---------------------------------------------------------------- без токена и без аренды

def test_server_does_not_start_without_token(monkeypatch):
    monkeypatch.delenv("GITS_CONTROL_TOKEN", raising=False)
    with pytest.raises(ControlError):
        ControlServer(id="rimworld")
    with pytest.raises(ControlError):
        ControlServer(id="rimworld", token="")


def test_token_from_environment(monkeypatch):
    monkeypatch.setenv("GITS_CONTROL_TOKEN", TOKEN)
    assert ControlServer(id="rimworld")._token == TOKEN


async def test_service_and_owned_have_status_only():
    for kwargs in ({"role": "service"}, {"role": "workspace", "ownership": "owned"}):
        async with ControlServer(id="pet", token=TOKEN, active=lambda: 2, **kwargs) as s:
            lc = LeaseClient(s.url, TOKEN)
            st = await lc.status()
            assert st["state"] == "free" and st["holder"] is None and st["active"] == 2
            assert st["role"] == kwargs["role"] and ("ownership" in st) == (kwargs["role"] == "workspace")
            for call in (acquire(lc), lc.renew("x"), lc.release("x")):
                with pytest.raises(LeaseError) as e:
                    await call
                assert e.value.status == 404


async def test_status_offline_for_service_without_lease():
    async with ControlServer(id="pet", token=TOKEN, role="service", available=lambda: False) as s:
        assert (await LeaseClient(s.url, TOKEN).status())["state"] == "offline"


# ---------------------------------------------------------------- клиент: ошибки

async def test_client_unavailable_and_timeout():
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(LeaseUnavailable):
        await LeaseClient(f"http://127.0.0.1:{port}", TOKEN, timeout=2).status()

    async def silent(reader, writer):                                # принимает соединение и молчит
        await asyncio.sleep(5)
        writer.close()
    srv = await asyncio.start_server(silent, "127.0.0.1", 0)
    try:
        with pytest.raises(LeaseUnavailable) as e:
            await LeaseClient("http://127.0.0.1:%d" % srv.sockets[0].getsockname()[1], TOKEN, timeout=0.2).status()
        assert "не ответил" in str(e.value)
    finally:
        srv.close()


# ---------------------------------------------------------------- контракт

def test_contract_describes_every_endpoint():
    assert [(e.method, e.path) for e in C.ENDPOINTS] == [("GET", "/status"), ("POST", "/acquire"), ("POST", "/renew"),
                                                         ("POST", "/release")]
    assert C.STATES == ("offline", "free", "busy")
    assert P.ENV_CONTROL_TOKEN == "GITS_CONTROL_TOKEN"
    for e in C.ENDPOINTS:
        assert e.request_schema()["type"] == "object" and e.response_schema()["type"] == "object"


async def test_examples_in_contract_are_accepted_by_server(lc, server):
    from gits_sdk.contract.spec import check_fields
    ex = C.ACQUIRE.example_request
    assert check_fields(C.ACQUIRE.request, ex) == []
    got = await lc.acquire(**ex)
    assert check_fields(C.ACQUIRE.response, got) == []
    assert check_fields(C.STATUS.response, await lc.status()) == []
    assert check_fields(C.RENEW.response, await lc.renew(got["lease_id"])) == []
    assert check_fields(C.RELEASE.response, await lc.release(got["lease_id"])) == []
