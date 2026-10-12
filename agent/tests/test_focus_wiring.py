"""Проводка менеджера фокуса в ядро (ТЗ 18): статус и политики из реестра модулей, команды фокуса голосом (инструмент
focus.control) и эндпоинты дашборда. Без процессов модулей, Claude и LM Studio."""
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import core_service
import dashboard
from environments import builtin
from focus_rigs import GAME_POLICIES, World


class FakeRegistry:
    """Реестр модулей с тем же get(), что у ModuleRegistry (records - записи по id)."""

    def __init__(self, records):
        self.records = records

    def get(self, module_id):
        return self.records.get(module_id)


def registry_with(**records):
    return FakeRegistry(dict(records))


def record(manifest=None, state=None, title="", rec_id="x"):
    return SimpleNamespace(id=rec_id, manifest=manifest, state=state, title=title)


def test_module_status_reads_status_from_state_data():
    rec = record(state={"summary": "в игре", "data": {"status": {"active_policy": "observe", "interruptible": True}}})
    reg = registry_with(rimworld=rec)
    assert core_service._module_status(reg, "rimworld") == {"active_policy": "observe", "interruptible": True}
    assert core_service._module_status(reg, "nope") is None


def test_module_status_absent_or_malformed_is_none():
    assert core_service._module_status(registry_with(a=record(state=None)), "a") is None
    assert core_service._module_status(registry_with(a=record(state={"data": {"status": "active"}})), "a") is None
    assert core_service._module_status(registry_with(a=record(state={"data": ["x"]})), "a") is None


def test_module_policies_absent_means_compatibility_mode():
    reg = registry_with(echo=record(manifest={"id": "echo", "tools": []}))
    assert core_service._module_policies(reg, "echo") is None
    assert core_service._module_policies(reg, "missing") is None


def test_module_policies_come_from_manifest():
    manifest = {"id": "rimworld", "policies": GAME_POLICIES["policies"], "default_policy": "afk"}
    reg = registry_with(rimworld=record(manifest=manifest))
    spec = core_service._module_policies(reg, "rimworld")
    assert spec["default_policy"] == "afk" and len(spec["policies"]) == 3


def test_module_title_falls_back_to_id():
    reg = registry_with(montage=record(title="Монтаж"), echo=record(title=""))
    assert core_service._module_title(reg, "montage") == "Монтаж"
    assert core_service._module_title(reg, "echo") == "echo"
    assert core_service._module_title(reg, "none") == "none"


async def test_focus_control_tool_pin_dnd_and_once():
    w = World(now=1000.0)
    w.add("rimworld", GAME_POLICIES)
    w.add("telegram", {"default_policy": "available", "policies": [
        {"id": "available", "title": "Доступна", "focus": "background"},
        {"id": "do_not_disturb", "title": "Не беспокоить", "focus": "background", "quiet": True}]})
    mgr = w.manager()
    tool = builtin._focus_tool(mgr, registry_with())
    assert tool.name == "control" and tool.id == "focus.control" and tool.risk == "auto"

    out = await tool.call({"action": "pin", "module": "rimworld"})
    assert "закреплён" in out and mgr.pinned == "rimworld" and mgr.slot.module == "rimworld"
    assert "снято" in await tool.call({"action": "unpin"}) and mgr.pinned is None

    assert "включено" in await tool.call({"action": "dnd_on"})
    assert mgr.dnd_all is True
    assert "выключено" in await tool.call({"action": "dnd_off", "module": "telegram"}) and mgr.dnd_all is True
    await tool.call({"action": "dnd_on", "module": "telegram"})
    assert "telegram" in mgr.dnd_modules
    assert "разовое" in await tool.call({"action": "allow_once", "module": "telegram"})
    assert "telegram" in mgr._once


async def test_focus_control_tool_requires_module_for_pin_and_once():
    mgr = World(now=1000.0).manager()
    tool = builtin._focus_tool(mgr, registry_with())
    with pytest.raises(builtin.ToolError):
        await tool.call({"action": "pin"})
    with pytest.raises(builtin.ToolError):
        await tool.call({"action": "allow_once"})


def test_focus_tool_registered_only_with_focus_manager():
    from environments.registry import Registry
    with_focus = Registry()
    builtin.register_core_tools(with_focus, registry_with(), attention=None, focus=World(now=1.0).manager())
    assert with_focus.get("focus.control") is not None
    without = Registry()
    builtin.register_core_tools(without, registry_with(), attention=None, focus=None)
    assert without.get("focus.control") is None


@pytest.fixture
async def focus_api():
    w = World(now=1000.0)
    w.add("rimworld", GAME_POLICIES)
    mgr = w.manager()
    dashboard.set_focus(mgr)
    client = TestClient(TestServer(dashboard.build_app()))
    await client.start_server()
    try:
        yield client, mgr
    finally:
        await client.close()
        dashboard.set_focus(None)


async def test_api_focus_get_returns_snapshot(focus_api):
    client, mgr = focus_api
    resp = await client.get("/api/focus")
    assert resp.status == 200
    body = await resp.json()
    assert body["holder"] is None and body["pin"] is None and body["queue"] == []
    assert "policies" in body and "journal" in body


async def test_api_focus_pin_dnd_once(focus_api):
    client, mgr = focus_api
    resp = await client.post("/api/focus/pin", json={"module": "rimworld"})
    assert resp.status == 200 and (await resp.json())["ok"] is True and mgr.pinned == "rimworld"
    resp = await client.post("/api/focus/pin", json={"module": None})
    assert (await resp.json())["pin"] is None and mgr.pinned is None
    resp = await client.post("/api/focus/dnd", json={"module": "all", "on": True})
    assert (await resp.json())["all"] is True
    resp = await client.post("/api/focus/allow_once", json={"module": "rimworld"})
    assert (await resp.json())["ok"] is True and "rimworld" in mgr._once


async def test_api_focus_rejects_bad_bodies(focus_api):
    client, _ = focus_api
    assert (await client.post("/api/focus/pin", json={"module": 5})).status == 400
    assert (await client.post("/api/focus/dnd", json={"module": "all", "on": "yes"})).status == 400
    assert (await client.post("/api/focus/allow_once", json={})).status == 400


async def test_api_focus_without_manager_answers_empty():
    dashboard.set_focus(None)
    client = TestClient(TestServer(dashboard.build_app()))
    await client.start_server()
    try:
        resp = await client.get("/api/focus")
        assert resp.status == 200 and (await resp.json())["holder"] is None
        resp = await client.post("/api/focus/pin", json={"module": "rimworld"})
        assert resp.status == 503
    finally:
        await client.close()

