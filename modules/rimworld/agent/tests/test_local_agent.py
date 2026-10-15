"""Локальный ход Gemma: схема инструментов режима, цикл вызовов, защита от повторов, смена режима в ходе."""
import asyncio
import json

import game_modes as gm
import local_agent as la


def _registry():
    calls = []

    def make(name):
        def fn(**kw):
            calls.append((name, kw))
            return f"OK {name} {kw}"
        return fn
    names = set(gm.CORE)
    for m in gm.MODES.values():
        names |= set(m.tools)
    return {n: (f"Описание {n}. Подробности.", {"type": "object", "properties": {}}, make(n)) for n in names}, calls


def _tool_call(name, args=None, i=1):
    return {"message": {"content": "", "tool_calls": [{"id": f"c{i}", "function": {"name": name, "arguments": json.dumps(args or {})}}]}}


def test_schemas_contain_only_the_modes_tools():
    reg, _ = _registry()
    names = {s["function"]["name"] for s in la.schemas("combat", reg)}
    assert names == set(gm.tools_for("combat")) and "draft_pawns" in names and "build_room" not in names
    assert all(len(s["function"]["description"]) <= 260 for s in la.schemas("build", reg))


def test_turn_runs_tools_then_finishes_with_text():
    reg, calls = _registry()
    script = [_tool_call("get_colony_state"), _tool_call("draft_pawns", {"pawn_ids": ["a"]}, 2), {"message": {"content": "Готово."}}]

    async def chat(messages, tools):
        assert tools and any(t["function"]["name"] == "attack_target" for t in tools)
        return script.pop(0)
    out = asyncio.run(la.run_local_turn("контекст", "combat", urgent=["налёт"], chat=chat, registry=reg))
    assert out["success"] and out["actions"] == ["get_colony_state", "draft_pawns"] and out["commentary"] == "Готово."
    assert [c[0] for c in calls] == ["get_colony_state", "draft_pawns"]


def test_repeat_call_is_refused_and_unknown_tool_reports_error():
    reg, calls = _registry()
    script = [_tool_call("get_colony_state"), _tool_call("get_colony_state", i=2), _tool_call("build_room", i=3), {"message": {"content": ""}}]

    async def chat(messages, tools):
        return script.pop(0)
    out = asyncio.run(la.run_local_turn("к", "observe", chat=chat, registry=reg))
    assert [c[0] for c in calls] == ["get_colony_state", "build_room"]            # повтор не исполнен
    assert out["success"]


def test_switch_mode_swaps_tools_inside_the_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "_REQUEST", tmp_path / "mode_request.json")
    reg, _ = _registry()

    def switch(**kw):
        gm.request_switch(kw["mode"], kw.get("reason", ""))
        return "OK: режим включится"
    reg["switch_mode"] = ("Сменить режим", {"type": "object", "properties": {}}, switch)
    seen_tools = []
    script = [_tool_call("switch_mode", {"mode": "build", "reason": "нужна комната"}), {"message": {"content": "ок"}}]

    async def chat(messages, tools):
        seen_tools.append({t["function"]["name"] for t in tools})
        return script.pop(0)
    out = asyncio.run(la.run_local_turn("к", "observe", chat=chat, registry=reg))
    assert "plan_room" not in seen_tools[0] and "plan_room" in seen_tools[1] and out["mode"] == "build"


def test_model_down_returns_unsuccessful_turn():
    reg, _ = _registry()

    async def chat(messages, tools):
        raise RuntimeError("нет связи")
    out = asyncio.run(la.run_local_turn("к", "observe", chat=chat, registry=reg))
    assert out["success"] is False and out["actions"] == []
