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
    assert "colony_assess" in tools and "colony_assess" in game_brief.GAME_BRIEF       # оценка колонии: с неё агент выбирает цель


def test_brief_makes_the_agent_set_and_close_goals_itself():
    text = game_brief.GAME_BRIEF
    assert "Цели ставишь сама" in text and "assistant.create_task" in text and "закрывай" in text
    assert "больше двух чтений подряд" in text and "pawn_orders_force_work" in text      # прежние правила хода сохранены


def test_with_entry_only_on_success():
    ok = controller._with_entry({"ok": True, "already": True})
    assert ok["entry"] == {"brief": game_brief.GAME_BRIEF} and ok["already"] is True
    assert "entry" not in controller._with_entry({"ok": False, "error": "x"})
