"""Автопереход к уже виденному узлу (workspace_module.py): первый вход в узел явным `open`, дальше действия узла можно звать
из любого другого узла, модуль сам переходит в него так же, как явный `open`, и пишет пометку в начале ответа."""
import inspect

import pytest
from gits_sdk import ToolError
from rigs import Rig

import controller
import gamenet
import rimworld_mcp
import workspace_module as wm
import workspace_tree as wt


def _fake_game(monkeypatch):
    """Подмена игры: журнал вызовов окна, выделения и действий. Возвращает (tabs, selected, done)."""
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    tabs, selected, done = [], [], []
    monkeypatch.setattr(rimworld_mcp, "open_game_tab", lambda tab: tabs.append(tab) or "OK")
    monkeypatch.setattr(rimworld_mcp, "select_target", lambda pawn_id=None, **k: selected.append(pawn_id) or "OK")
    monkeypatch.setattr(rimworld_mcp, "_query", lambda cmd: "OK")
    monkeypatch.setattr(rimworld_mcp, "ui_state", lambda: '{"open_tab": "Work"}')
    monkeypatch.setattr(rimworld_mcp, "draft_pawns", lambda pawn_ids: done.append(("draft", list(pawn_ids))) or "OK: draft")
    monkeypatch.setattr(rimworld_mcp, "move_pawn", lambda pawn_id, x, z: done.append(("move", pawn_id)) or "OK: move")
    monkeypatch.setattr(rimworld_mcp, "equip_item", lambda pawn_id, item_id: done.append(("equip", pawn_id)) or "OK: equip")
    monkeypatch.setattr(rimworld_mcp, "set_work_priorities",
                        lambda pawn_id, priorities: done.append(("work", pawn_id)) or "OK: work")
    return tabs, selected, done


async def test_seen_node_is_entered_automatically_with_a_note(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "pawn/orders", "id": "Human1"})
        await rig.core.call_tool("open", {"path": "pawn/gear", "id": "Human1"})
        assert rig.node_state.path == "pawn/gear"
        reply = await rig.core.call_tool("pawn_orders_draft", {"pawn_ids": ["Human1", "Human2"]})
        assert reply == "(перешла в узел pawn/orders)\nOK: draft" and done == [("draft", ["Human1", "Human2"])]
        assert rig.node_state.path == "pawn/orders" and rig.node_state.target == "Human1"      # переход настоящий
        here = await rig.core.call_tool("where", {})                                           # прежний узел не остался открытым
        assert here["node"] == "pawn/orders" and "pawn_gear_equip" not in here["actions"]
        assert await rig.core.call_tool("pawn_orders_move", {"pawn_id": "Human1", "x": 1, "z": 2}) == "OK: move"   # без пометки


async def test_auto_entry_switches_the_game_window_like_an_explicit_open(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "work"})
        await rig.core.call_tool("open", {"path": "pawn/gear", "id": "Human1"})
        tabs.clear()
        reply = await rig.core.call_tool("work_set_priorities", {"pawn_id": "Human1", "priorities": {"Cooking": 1}})
        assert reply.startswith("(перешла в узел work)") and tabs[0] == "work" and rig.node_state.path == "work"
        tabs.clear()
        await rig.core.call_tool("open", {"path": "map"})                  # узел без своего окна: окно «Работа» закрывается
        assert tabs == ["close"] and rig.node_state.path == "map"
        await rig.core.call_tool("pawn_gear_equip", {"pawn_id": "Human1", "item_id": "i1"})
        assert selected[-1] == "Human1" and rig.node_state.path == "pawn/gear"


async def test_unseen_node_keeps_the_error_with_an_exact_open_call(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("pawn_orders_move", {"pawn_id": "Human53845", "x": 1, "z": 2})
        assert e.value.code == "bad_request" and 'open("pawn/orders", id="Human53845")' in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("pawn_orders_draft", {"pawn_ids": ["Human7", "Human8"]})
        assert 'open("pawn/orders", id="Human7")' in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("work_set_priorities", {"pawn_id": "p1", "priorities": {}})
        assert 'open("work")' in e.value.message
        assert done == [] and selected == [] and rig.node_state.path == "root"


async def test_changing_the_pawn_reopens_the_seen_node(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "pawn/gear", "id": "Human1"})
        assert await rig.core.call_tool("pawn_gear_equip", {"pawn_id": "Human1", "item_id": "i"}) == "OK: equip"
        reply = await rig.core.call_tool("pawn_gear_equip", {"pawn_id": "Human2", "item_id": "i"})
        assert reply == "(перешла в узел pawn/gear)\nOK: equip" and selected == ["Human1", "Human2"]
        assert rig.node_state.target == "Human2"


async def test_parametric_node_without_a_pawn_in_arguments_keeps_the_error(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "pawn/orders", "id": "Human1"})
        await rig.core.call_tool("open", {"path": "map"})
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("pawn_orders_draft", {"pawn_ids": []})
        assert 'open("pawn/orders")' in e.value.message and rig.node_state.path == "map"


async def test_nodes_in_no_auto_open_are_not_entered(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    monkeypatch.setattr(wt, "NO_AUTO_OPEN", frozenset({"work"}))
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "work"})
        await rig.core.call_tool("open", {"path": "map"})
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("work_set_priorities", {"pawn_id": "p1", "priorities": {}})
        assert 'open("work")' in e.value.message and rig.node_state.path == "map" and done == []


async def test_seen_nodes_are_forgotten_on_reset(tmp_path, monkeypatch):
    tabs, selected, done = _fake_game(monkeypatch)
    async with Rig(tmp_path) as rig:
        await rig.core.call_tool("open", {"path": "pawn/orders", "id": "Human1"})
        await rig.core.call_tool("open", {"path": "map"})
        rig.node_state.forget_seen()
        assert rig.node_state.seen == set()
        with pytest.raises(ToolError):
            await rig.core.call_tool("pawn_orders_move", {"pawn_id": "Human1", "x": 1, "z": 2})
        assert done == []


def test_controller_resets_nodes_on_lease_exit_and_new_game():
    c = controller.Controller.__new__(controller.Controller)
    c.on_nodes_reset = None
    c._reset_nodes()                                  # без хука не падает
    calls = []
    c.on_nodes_reset = lambda: calls.append(1)
    c._reset_nodes()
    assert calls == [1]
    src = inspect.getsource(controller.Controller)
    assert src.count("self._reset_nodes()") >= 3      # _take_focus (аренда), _drop_focus (выход), game_loaded (новая игра)


def test_pawn_id_comes_from_pawn_id_or_the_first_of_pawn_ids():
    assert wm.pawn_id_from({"pawn_id": " A "}) == "A"
    assert wm.pawn_id_from({"pawn_ids": ["B", "C"]}) == "B"
    assert wm.pawn_id_from({"pawn_ids": "C, D"}) == "C"
    assert wm.pawn_id_from({"pawn_ids": '["E"]'}) == "E"
    assert wm.pawn_id_from({"pawn_ids": []}) == ""
