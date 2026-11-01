"""Рабочее место RimWorld (workspace_module.py): манифест по дереву, навигация, отказ действия вне узла, запросы без узла."""
import pytest
from gits_sdk import ToolError
from gits_sdk import protocol as P
from rigs import Rig

import gamenet
import module_manifest
import rimworld_mcp
import workspace_module as wm
import workspace_tree as wt


async def test_manifest_nodes_tools_and_context_are_consistent():
    manifest = await module_manifest.build()
    assert P.validate_manifest(manifest) == []
    paths = {n["path"] for n in manifest["nodes"]}
    assert paths == {n.path for n in wt.NODES}
    names = {t["name"] for t in manifest["tools"]}
    for tool in manifest["tools"]:
        if tool.get("node"):
            assert tool["node"] in paths
    for node in manifest["nodes"]:
        for item in node.get("context") or []:
            assert item["tool"] in names                                   # панель узла ссылается на существующие запросы
    arch = next(n for n in manifest["nodes"] if n["path"] == "architect")
    assert [c["tool"] for c in arch["context"]] == ["colony_resources", "colony_buildings"]
    assert arch["watch"] == {"resources": 1, "threat": 1, "social": -1}


async def test_events_declare_topics_urgency_unfocused_and_follow_up():
    manifest = await module_manifest.build()
    names = {t["name"] for t in manifest["tools"]}
    emits = {e["kind"]: e for e in manifest["emits"]}
    assert emits["rimworld.raid"]["topics"] == ["threat"] and emits["rimworld.raid"]["default_urgency"] == 5
    assert emits["rimworld.letter"]["urgency_unfocused"] == 1
    for e in emits.values():
        assert set(e.get("follow_up") or []) <= names


async def test_global_actions_have_no_node_and_node_actions_name_their_node():
    tools = {t["name"]: t for t in (await module_manifest.build())["tools"]}
    for name in ("pause", "resume", "set_speed", "open", "back", "where", "colony_resources"):
        assert "node" not in tools[name]
    assert tools["architect_zone_stockpile"]["node"] == "architect/zone"
    assert tools["work_set_priorities"]["node"] == "work"


async def test_action_outside_its_node_is_refused_with_the_node_to_open(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    called = []

    def fake(pawn_id: str, priorities: dict) -> str:
        called.append({"pawn_id": pawn_id, "priorities": priorities})
        return "OK"
    monkeypatch.setattr(rimworld_mcp, "set_work_priorities", fake)
    async with Rig(tmp_path) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("work_set_priorities", {"pawn_id": "p1", "priorities": {"Cooking": 1}})
        assert e.value.code == "bad_request" and 'open("work")' in e.value.message and "root" in e.value.message
        assert called == []                                                 # до игры вызов не дошёл
        rig.node_state.set("work")
        assert await rig.core.call_tool("work_set_priorities", {"pawn_id": "p1", "priorities": {"Cooking": 1}}) == "OK"
        assert called == [{"pawn_id": "p1", "priorities": {"Cooking": 1}}]


async def test_queries_work_in_any_node(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(rimworld_mcp, "get_resources", lambda: "дерево 120")
    async with Rig(tmp_path) as rig:
        for node in ("root", "work", "pawn/orders"):
            rig.node_state.set(node)
            assert await rig.core.call_tool("colony_resources", {}) == "дерево 120"


async def test_open_back_and_where_follow_the_game_interface(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    tabs, queries, selected = [], [], []
    monkeypatch.setattr(rimworld_mcp, "open_game_tab", lambda tab: tabs.append(tab) or "OK")
    monkeypatch.setattr(rimworld_mcp, "select_target", lambda pawn_id=None, **k: selected.append(pawn_id) or "OK")
    monkeypatch.setattr(rimworld_mcp, "_query", lambda cmd: queries.append(cmd) or "OK")
    monkeypatch.setattr(rimworld_mcp, "ui_state", lambda: '{"tab": "Architect", "category": "Zone"}')
    changes = []
    async with Rig(tmp_path) as rig:
        rig.node_state.on_change = lambda: changes.append(rig.node_state.path)
        listing = await rig.core.call_tool("open", {})
        assert listing["node"] == "root" and "architect" in listing["children"]
        reply = await rig.core.call_tool("open", {"path": "architect/zone"})
        assert reply["node"] == "architect/zone" and tabs == ["architect"] and queries == [{"type": "ui_category", "id": "Zone"}]
        here = await rig.core.call_tool("where", {})
        assert here["node"] == "architect/zone" and "architect_zone_stockpile" in here["actions"]
        assert here["ui"] == {"tab": "Architect", "category": "Zone"}
        assert (await rig.core.call_tool("back", {}))["node"] == "architect"
        assert (await rig.core.call_tool("back", {}))["node"] == "root" and tabs[-1] == "close"
        await rig.core.call_tool("back", {})                                # с корня: ничего
        await rig.core.call_tool("open", {"path": "pawn", "id": "p7"})
        assert selected == ["p7"] and rig.node_state.path == "pawn"
        assert changes == ["architect/zone", "architect", "root", "pawn"]    # каждая смена узла уходит ядру сразу


async def test_open_rejects_unknown_nodes_and_pawn_nodes_without_a_pawn(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    async with Rig(tmp_path) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("open", {"path": "нет/такого"})
        assert e.value.code == "bad_request" and "architect" in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("open", {"path": "pawn/orders"})
        assert 'id' in e.value.message and rig.node_state.path == "root"


def test_every_old_function_behind_a_tool_exists_in_the_mcp_server():
    missing = [old for name, old in wm.OLD_BY_NAME.items() if not callable(getattr(rimworld_mcp, old, None))]
    assert missing == []
