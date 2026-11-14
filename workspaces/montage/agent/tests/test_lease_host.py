"""Режим аренды монтажа: сервис ждёт аренды, подключается к ядру хозяина по acquire, отключается по release и обрыву связи."""
import asyncio

import pytest
from gits_sdk.control import ControlServer, LeaseBusy, LeaseClient
from gits_sdk.testing import FakeCore

import lease_host
import main as main_mod
from layout import Layout
from manifest import MANIFEST
from tests.stubs import FakeRenderer, StubQA, layout_for

pytestmark = pytest.mark.usefixtures("fast")
TOKEN = "control-token"


def test_enabled_conditions():
    assert lease_host.enabled({"GITS_CONTROL_TOKEN": "t"})
    assert not lease_host.enabled({})                                         # без токена прежний путь
    assert not lease_host.enabled({"GITS_CONTROL_TOKEN": "t", "GITS_CORE_URL": "ws://x/ws"})
    assert lease_host.enabled({"GITS_CONTROL_TOKEN": "t", "GITS_CORE_URL": "ws://x/ws", "GITS_LEASE_MODE": "1"})


def test_layout_for_owner(tmp_path):
    base = layout_for(tmp_path)
    own = base.for_owner("glados", tmp_path / "chars")
    assert own.character == tmp_path / "chars" / "glados" / "montage"
    assert own.jobs == own.character / "jobs" and own.assets == own.character / "assets"
    for bad in ("", "..", "a/b", "a\b", "../x"):
        with pytest.raises(ValueError):
            base.for_owner(bad, tmp_path)


async def make_host(tmp_path, root=None):
    base = layout_for(tmp_path)
    built = []

    def build_fn(layout: Layout, *, url, token):
        client, manager = main_mod.build(layout, url=url, token=token, renderer=FakeRenderer(), qa=StubQA(), site_port=0)
        built.append((layout, client, manager))
        return client, manager

    host = lease_host.LeaseHost(build_fn, main_mod.run, layout=base, characters_root=root)
    server = ControlServer(id="montage", token=TOKEN, ownership="shared", manifest=MANIFEST,
                           on_acquire=host.acquire, on_release=host.release, jobs=host.active_jobs)
    host.server = server
    await server.start()
    return host, server, built


async def test_idle_without_core_then_acquire_release(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path, root=tmp_path / "chars")
        try:
            lc = LeaseClient(server.url, TOKEN)
            assert built == [] and core.connections == 0                      # на старте к ядру не подключается
            info = await lc.info()
            assert info["id"] == "montage" and info["when_to_use"] and info["state"] == "free"

            got = await lc.acquire(character="glados", core_url=core.url, token="core-token", purpose="montage")
            assert got["ok"] and core.connections == 1 and (await lc.status())["state"] == "busy"
            layout, client, manager = built[0]
            assert layout.character == tmp_path / "chars" / "glados" / "montage"        # ресурсы хозяина, не чужие
            assert client.connected
            with pytest.raises(LeaseBusy):
                await lc.acquire(character="other", core_url=core.url, token="core-token")

            assert (await lc.release(got["lease_id"]))["released"] is True       # задач нет: освобождается сразу
            assert not client.connected
            assert (await lc.status())["state"] == "free"
        finally:
            await server.stop()


async def test_core_drop_releases_lease(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path)
        try:
            lc = LeaseClient(server.url, TOKEN)
            await lc.acquire(character="glados", core_url=core.url, token="core-token")
            await core.wait_connected()
            for conn in list(core._conns):
                await conn.ws.close()                                         # ядро хозяина упало
            for _ in range(200):
                if (await lc.status())["state"] == "free":
                    break
                await asyncio.sleep(0.02)
            assert (await lc.status())["state"] == "free"
            got = await lc.acquire(character="other", core_url=core.url, token="core-token")   # место свободно
            assert got["ok"]
        finally:
            await server.stop()


async def test_acquire_fails_when_core_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(lease_host, "CONNECT_TIMEOUT_S", 0.3)
    host, server, built = await make_host(tmp_path)
    host.connect_timeout = 0.3
    try:
        lc = LeaseClient(server.url, TOKEN)
        with pytest.raises(Exception) as e:
            await lc.acquire(character="glados", core_url="ws://127.0.0.1:9/ws", token="x")
        assert getattr(e.value, "status", None) == 502
        assert (await lc.status())["state"] == "free"
    finally:
        await server.stop()


def _running(manager, run_id="j-run"):
    """Операция идёт: запись в реестре менеджера (настоящая фоновая операция проверена в test_workspace_tools)."""
    from manager import Run
    manager._runs[run_id] = Run(run_id=run_id, job_id="j-00000000", command="render", background=True)
    return run_id


async def _wait(pred, n=200):
    for _ in range(n):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_release_with_active_job_keeps_lease_then_finishes(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path)
        try:
            lc = LeaseClient(server.url, TOKEN)
            got = await lc.acquire(character="glados", core_url=core.url, token="core-token", purpose="montage")
            _, client, manager = built[0]
            _running(manager, "j-fake")                                           # идёт операция
            cancelled = []

            async def fake_cancel(job_id):
                cancelled.append(job_id)
            manager.cancel = fake_cancel
            res = await lc.release(got["lease_id"])
            assert res == {"ok": True, "released": False, "pending": "jobs"}
            st = await lc.status()
            assert st["state"] == "busy" and st["holder"]["releasing"] and st["holder"]["jobs"] == 1
            assert client.connected and cancelled == []                          # связь осталась, задачи не отменены
            with pytest.raises(LeaseBusy):
                await lc.acquire(character="other", core_url=core.url, token="core-token")
            manager._runs.clear()                                                # операция кончилась
            manager.on_idle()
            assert await _wait(lambda: not client.connected)
            assert await _wait(lambda: server.lease is None)
            assert (await lc.status())["state"] == "free" and cancelled == []
        finally:
            await server.stop()


async def test_same_character_returns_while_releasing(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path)
        try:
            lc = LeaseClient(server.url, TOKEN)
            got = await lc.acquire(character="glados", core_url=core.url, token="core-token", purpose="montage")
            _, client, manager = built[0]
            _running(manager, "j-fake")
            await lc.release(got["lease_id"])
            back = await lc.acquire(character="glados", core_url=core.url, token="core-token", purpose="montage")
            assert back["lease_id"] == got["lease_id"] and "releasing" not in (await lc.status())["holder"]
            manager._runs.clear()
            manager.on_idle()                                                    # возврат отменил выход: освобождения нет
            await asyncio.sleep(0.2)
            assert client.connected and (await lc.status())["state"] == "busy"
        finally:
            await server.stop()


async def test_core_drop_cancels_active_jobs_even_while_releasing(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path)
        try:
            lc = LeaseClient(server.url, TOKEN)
            got = await lc.acquire(character="glados", core_url=core.url, token="core-token")
            await core.wait_connected()
            _, client, manager = built[0]
            _running(manager, "j-fake")
            cancelled = []

            async def fake_cancel(job_id):
                cancelled.append(job_id)
            manager.cancel = fake_cancel
            await lc.release(got["lease_id"])
            for conn in list(core._conns):
                await conn.ws.close()
            assert await _wait(lambda: server.lease is None)
            assert cancelled == ["j-fake"]
        finally:
            await server.stop()


# ---------------------------------------------------------------- единицы на диске переживают возврат аренды; afk аренду не трогает

async def test_core_drop_cancels_running_operations_but_keeps_units(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path)
        try:
            lc = LeaseClient(server.url, TOKEN)
            await lc.acquire(character="glados", core_url=core.url, token="core-token")
            await core.wait_connected()
            _, client, manager = built[0]
            unit = manager.store.create({"sources": [], "brief": "ролик", "platform": "none"})
            _running(manager, "j-run")
            cancelled = []

            async def fake_cancel(job_id):
                cancelled.append(job_id)
            manager.cancel = fake_cancel
            for conn in list(core._conns):
                await conn.ws.close()
            assert await _wait(lambda: server.lease is None)
            assert cancelled == ["j-run"] and manager.store.load(unit.job_id) is not None
        finally:
            await server.stop()


async def test_unit_returns_on_next_entry_and_afk_keeps_the_lease(tmp_path):
    async with FakeCore(token="core-token") as core:
        host, server, built = await make_host(tmp_path, root=tmp_path / "chars")
        try:
            lc = LeaseClient(server.url, TOKEN)
            got = await lc.acquire(character="glados", core_url=core.url, token="core-token")
            _, client, manager = built[0]
            await core.wait_connected()
            res = await core.request("workspace.afk", {"reason": "render"})            # отошла: аренда и связь на месте
            assert res["ok"] and client.connected and (await lc.status())["state"] == "busy"
            unit = manager.store.create({"sources": [], "brief": "ролик", "platform": "none"})
            assert (await lc.release(got["lease_id"]))["released"] is True             # операций нет: освобождается сразу
            await lc.acquire(character="glados", core_url=core.url, token="core-token")
            manager2 = built[1][2]
            assert await _wait(lambda: unit.job_id in manager2._jobs)                  # recover вернул единицу
        finally:
            await server.stop()
