"""Вводная агента (entry.brief) в ответах workspace.enter/resume и соответствие названий инструментов в ней дереву узлов."""
import re

import controller
import game_brief
import workspace_module as wm


def test_brief_is_short_and_names_existing_tools():
    assert len(game_brief.GAME_BRIEF) <= game_brief.BRIEF_MAX == 800
    tools = {name for _old, _kind, _path, name, _node in wm._specs_table()}
    mentioned = set(re.findall(r"\b(?:architect|work|pawn|inspect)_[a-z_]+\b", game_brief.GAME_BRIEF))
    assert {"architect_zone_stockpile", "inspect_unforbid_area", "architect_structure_build_room", "architect_fit_room",
            "architect_zone_grow_zone", "pawn_orders_force_work", "work_set_priorities", "architect_orders_plan_room",
            "architect_structure_place_building"} <= mentioned
    assert mentioned <= tools, mentioned - tools


def test_with_entry_only_on_success():
    ok = controller._with_entry({"ok": True, "already": True})
    assert ok["entry"] == {"brief": game_brief.GAME_BRIEF} and ok["already"] is True
    assert "entry" not in controller._with_entry({"ok": False, "error": "x"})
