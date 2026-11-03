"""Манифест модуля и обработчики инструментов против FakeCore (ТЗ 11, RW-10, RW-11, RW-14a, RW-20)."""
import pytest
from gits_sdk import ToolError
from gits_sdk import protocol as P
from rigs import Rig

import gamenet
import module_manifest
import rimworld_mcp


async def test_manifest_is_valid_and_built_from_the_workspace_tree():
    manifest = await module_manifest.build()
    assert P.validate_manifest(manifest) == []
    assert manifest["id"] == "rimworld" and manifest["capabilities"] == ["game"]
    assert manifest["graph"] is True and manifest["state"] is True
    tools = {t["name"]: t for t in manifest["tools"]}
    assert {"open", "back", "where", "colony_overview", "work_set_priorities", "pause"} <= set(tools)
    # обходы, дубли и мета-инструменты в рабочее место не попадают (WS-3, WS-4, WS-5)
    for gone in ("game_screenshot", "get_pending_events", "ui_steps", "pc_read_screen", "switch_mode", "set_work_priority",
                 "set_schedule_range", "set_goal", "web_search", "get_colony_state"):
        assert gone not in tools, gone
    assert len(tools) < 90
    for t in tools.values():
        assert t["risk"] == "auto" and t["availability"] == "profile"
        assert 0 < len(t["description"]) <= 300 and "keywords" in t
    assert tools["colony_overview"]["timeout_s"] == 60
    assert "order" not in tools                                              # приказов игре нет: агент действует инструментами узлов


async def test_shared_settings_emits_and_settings_schema():
    manifest = await module_manifest.build()
    assert manifest["shared_settings"] == ["streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted",
                                           "telegram_after_run", "record_runs"]
    emits = {e["kind"]: e["default_urgency"] for e in manifest["emits"]}
    assert emits == {"rimworld.colonists": 0, "rimworld.run_ended": 3, "rimworld.raid": 5, "rimworld.death": 4,
                     "rimworld.fire": 4, "rimworld.letter": 2, "rimworld.goal": 3, "rimworld.run_video": 2, "rimworld.game_started": 1,
                     "rimworld.pawn_downed": 4, "rimworld.enemy_seen": 3, "rimworld.enemy_down": 2, "rimworld.research_done": 2,
                     "rimworld.resource_out": 3, "rimworld.pawn_mood": 3, "rimworld.pawn_hurt": 3, "rimworld.pawn_lost": 4,
                     "rimworld.newcomer": 3}
    assert manifest["settings_schema"]["auto_montage"]["default"] is False
    import settings
    assert set(manifest["shared_settings"]) <= set(settings.DEFAULTS)         # общие настройки есть и в настройках модуля


def test_schema_to_params_handles_optional_and_enum():
    schema = {"properties": {"x": {"anyOf": [{"type": "integer"}, {"type": "null"}], "title": "X"},
                             "mode": {"type": "string", "enum": ["a", "b"], "description": "режим"},
                             "n": {"type": "integer"}}, "required": ["mode"]}
    params = module_manifest.schema_to_params(schema)
    assert params["x"] == {"type": "integer", "description": "X", "required": False}
    assert params["mode"] == {"type": "string", "description": "режим", "required": True, "enum": ["a", "b"]}
    assert params["n"]["required"] is False
    assert module_manifest.first_line("\n  Первая строка.\nвторая", "x") == "Первая строка."
    assert module_manifest.first_line("", "запас") == "запас"


async def test_every_tool_has_a_handler_and_core_connects(tmp_path):
    async with Rig(tmp_path) as rig:                     # run() бросил бы RuntimeError, если у инструмента нет обработчика
        hello = rig.core.hellos[0]
        assert hello["module_id"] == "rimworld"
        assert {t["name"] for t in hello["manifest"]["tools"]} == {t["name"] for t in rig.tools}


async def test_tool_call_runs_the_mcp_function_and_returns_its_string(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    seen = {}

    def fake_state():
        return "КОЛОНИЯ: 3 колониста"

    def fake_move(pawn_id: str, x: int, z: int) -> str:
        seen.update(pawn_id=pawn_id, x=x, z=z)
        return "OK: move"

    monkeypatch.setattr(rimworld_mcp, "get_colony_state", fake_state)
    monkeypatch.setattr(rimworld_mcp, "move_pawn", fake_move)
    async with Rig(tmp_path) as rig:
        assert await rig.core.call_tool("rimworld.colony_overview", {}) == "КОЛОНИЯ: 3 колониста"
        rig.node_state.set("pawn/orders")                                        # действие узла делается в своём узле
        # планировщик может передать число строкой: аргументы приводятся к типам функции
        assert await rig.core.call_tool("pawn_orders_move", {"pawn_id": "p1", "x": "4", "z": 7, "лишнее": 1}) == "OK: move"
        assert seen == {"pawn_id": "p1", "x": 4, "z": 7}
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("pawn_orders_move", {"pawn_id": "p1", "x": "не число", "z": 7})
        assert e.value.code == "bad_request"


async def test_tool_call_is_unavailable_without_the_game(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: False)
    called = []
    monkeypatch.setattr(rimworld_mcp, "get_colony_state", lambda: called.append(1) or "x")
    async with Rig(tmp_path) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("colony_overview", {})
        assert e.value.code == "unavailable" and "не запущена" in e.value.message
    assert called == []                                                   # к функции не обращались


async def test_blocking_function_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """Функции MCP-сервера синхронные: вызываются в потоке, цикл событий модуля не стоит."""
    import threading
    import time
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)
    threads = []

    def slow():
        threads.append(threading.current_thread())
        time.sleep(0.2)
        return "готово"

    monkeypatch.setattr(rimworld_mcp, "get_colony_state", slow)
    async with Rig(tmp_path) as rig:
        assert await rig.core.call_tool("colony_overview", {}) == "готово"
    assert threads and threads[0] is not threading.main_thread()


async def test_graph_get_returns_game_modes_graph(tmp_path):
    async with Rig(tmp_path) as rig:
        graph = await rig.core.request("graph.get", {})
        assert graph["nodes"] and graph["edges"] and graph["current"]
        assert {"agent", "observe", "build"} <= {n["id"] for n in graph["nodes"]}
