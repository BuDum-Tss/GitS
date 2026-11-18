"""Режим аренды RimWorld в контейнере: сервис ждёт аренды, подключается к ядру хозяина по acquire, отпускает по release и обрыву.
Без Docker и сети: ядро это `FakeCore` из SDK, клиент настоящий `WorkspaceClient` с манифестом модуля."""
import asyncio
import logging

import pytest
from gits_sdk import WorkspaceClient
from gits_sdk.control import ControlServer, LeaseBusy, LeaseClient, LeaseError
from gits_sdk.testing import FakeCore

import lease_host
import module_manifest

TOKEN = "control-token"
CORE_TOKEN = "core-token-secret"


def test_enabled_conditions():
    assert lease_host.enabled({"GITS_CONTROL_TOKEN": "t", "GITS_RIMWORLD_CONTAINER": "1"})
    assert lease_host.enabled({"GITS_CONTROL_TOKEN": "t", "GITS_LEASE_MODE": "1"})
    assert not lease_host.enabled({})
    assert not lease_host.enabled({"GITS_RIMWORLD_CONTAINER": "1"})              # без токена управления аренды нет
    assert not lease_host.enabled({"GITS_CONTROL_TOKEN": "t"})                   # одного токена мало (хост: он лежит в общем .env)
    assert not lease_host.enabled({"GITS_CONTROL_TOKEN": "t", "GITS_RIMWORLD_CONTAINER": "0"})


def manifest() -> dict:
    m = module_manifest.build_manifest([])
    m["tools"] = []                                  # без launch_game: обработчиков инструментов у этого клиента нет
    return m


async def make_host(root=None):
    built = []

    def build_fn(*, url, token):
        client = WorkspaceClient(manifest(), url=url, token=token)
        built.append((url, token, client))
        return client, {"ctx": True}

    async def run_fn(client, ctx):
        assert ctx == {"ctx": True}
        return await client.run()

    host = lease_host.LeaseHost(build_fn, run_fn, characters_root=root, connect_timeout=5.0, stop_timeout=3.0)
    server = ControlServer(id="rimworld", token=TOKEN, ownership="shared", manifest=manifest(),
                           on_acquire=host.acquire, on_release=host.release)
    host.server = server
    await server.start()
    return host, server, built


async def _free(lc, n=200):
    for _ in range(n):
        if (await lc.status())["state"] == "free":
            return True
        await asyncio.sleep(0.02)
    return False


async def test_idle_without_core_then_acquire_release(caplog):
    caplog.set_level(logging.DEBUG)
    async with FakeCore(token=CORE_TOKEN) as core:
        host, server, built = await make_host()
        try:
            lc = LeaseClient(server.url, TOKEN)
            assert built == [] and core.connections == 0                      # на старте к ядру не подключается
            info = await lc.info()
            assert info["id"] == "rimworld" and info["state"] == "free"

            got = await lc.acquire(character="glados", core_url=core.url, token=CORE_TOKEN, purpose="play")
            assert got["ok"] and core.connections == 1 and (await lc.status())["state"] == "busy"
            url, token, client = built[0]
            assert url == core.url and token == CORE_TOKEN                    # подключились к ядру хозяина из acquire
            assert client.connected
            with pytest.raises(LeaseBusy):
                await lc.acquire(character="other", core_url=core.url, token=CORE_TOKEN)

            assert (await lc.release(got["lease_id"]))["released"] is True
            assert not client.connected
            assert (await lc.status())["state"] == "free"
        finally:
            await server.stop()
    assert CORE_TOKEN not in caplog.text and TOKEN not in caplog.text         # токены в журнал не попадают


async def test_core_drop_releases_lease():
    async with FakeCore(token=CORE_TOKEN) as core:
        host, server, built = await make_host()
        try:
            lc = LeaseClient(server.url, TOKEN)
            await lc.acquire(character="glados", core_url=core.url, token=CORE_TOKEN)
            await core.wait_connected()
            for conn in list(core._conns):
                await conn.ws.close()                                         # ядро хозяина упало
            assert await _free(lc)
            assert (await lc.acquire(character="other", core_url=core.url, token=CORE_TOKEN))["ok"]   # место свободно
        finally:
            await server.stop()


async def test_acquire_fails_when_core_unreachable():
    host, server, built = await make_host()
    host.connect_timeout = 0.3
    try:
        lc = LeaseClient(server.url, TOKEN)
        with pytest.raises(Exception) as e:
            await lc.acquire(character="glados", core_url="ws://127.0.0.1:9/ws", token="x")
        assert getattr(e.value, "status", None) == 502
        assert (await lc.status())["state"] == "free" and host.client is None
    finally:
        await server.stop()


async def test_character_dir_env_follows_lease(monkeypatch):
    monkeypatch.delenv(lease_host.ENV_CHARACTER_DIR, raising=False)
    async with FakeCore(token=CORE_TOKEN) as core:
        host, server, built = await make_host(root="/characters")
        try:
            lc = LeaseClient(server.url, TOKEN)
            got = await lc.acquire(character="glados", core_url=core.url, token=CORE_TOKEN)
            assert lease_host.os.environ[lease_host.ENV_CHARACTER_DIR].replace("\\", "/") == "/characters/glados"
            await lc.release(got["lease_id"])
            assert lease_host.ENV_CHARACTER_DIR not in lease_host.os.environ
            with pytest.raises(LeaseError):                                   # id с разделителем пути отвергается
                await lc.acquire(character="../x", core_url=core.url, token=CORE_TOKEN)
        finally:
            await server.stop()
