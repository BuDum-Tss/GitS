"""Режимы игрового агента: инструменты разделены строго (каждый принадлежит одному режиму), выбор режима, смена по просьбе."""
import re
from collections import Counter
from pathlib import Path

import game_modes as gm


def _server_tools() -> set[str]:
    src = (Path(__file__).resolve().parent.parent / "rimworld_mcp.py").read_text(encoding="utf-8")
    return set(re.findall(r"@mcp\.tool\(\)\s*\n(?:async )?def (\w+)\(", src))


def test_every_tool_belongs_to_exactly_one_mode_or_core():
    server = _server_tools()
    listed = list(gm.CORE) + [t for m in gm.MODES.values() for t in m.tools]
    assert not (set(listed) - server), f"в режимах есть несуществующие инструменты: {set(listed) - server}"
    assert not (server - set(listed)), f"инструмент без режима: {server - set(listed)}"
    assert [t for t, n in Counter(listed).items() if n > 1] == [], "инструмент в двух режимах или в базовых и режиме"


def test_mode_lists_are_short_and_core_has_switch():
    # ТЗ 11e: к базовым добавлены запросы мира (колонисты, ресурсы) и switch_policy; порог поднят с 12 до 14 осознанно.
    # Исследования (unlock_for, закрытые постройки): порог режимов поднят с 32 до 33 осознанно, режим build стал 33 инструмента.
    assert "switch_mode" in gm.CORE and len(gm.CORE) <= 14
    for m in gm.MODES.values():
        assert len(gm.tools_for(m.id)) <= 33, m.id
    assert gm.home_mode("build_room") == "build" and gm.home_mode("get_colony_state") is None


def test_pick_mode_rules():
    calm = {"colony": {"pawns": [{"mood_pct": 0.7}], "food_days": 20}}
    assert gm.pick_mode(calm) == "observe"
    assert gm.pick_mode({**calm, "threats": [{"x": 1}]}) == "combat" and gm.pick_mode(calm, urgent=["налёт"]) == "combat"
    assert gm.pick_mode(calm, order="построй спальню") == "build" and gm.pick_mode(calm, order="расставь приоритеты работ") == "people"
    assert gm.pick_mode({"colony": {"pawns": [{"is_downed": True}]}}) == "health"
    assert gm.pick_mode({"colony": {"pawns": [], "pending_construction": [1]}}) == "build"
    assert gm.pick_mode({"colony": {"pawns": [], "food_days": 3}}) == "economy"
    assert gm.pick_mode(calm, tasks_text="- [pending] построить стену вокруг лагеря") == "build"


def test_switch_request_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "_REQUEST", tmp_path / "mode_request.json")
    assert gm.request_switch("nonsense") and gm.take_request() is None
    assert gm.request_switch("build", "нужна комната") is None
    req = gm.take_request()
    assert req["mode"] == "build" and req["reason"] == "нужна комната"
    assert gm.take_request() is None                      # просьба одноразовая


def test_mcp_server_restricts_to_mode(monkeypatch):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import rimworld_mcp as m
    names = {t.name for t in m.mcp._tool_manager.list_tools()}
    try:
        m._restrict_to_mode("combat")
        left = {t.name for t in m.mcp._tool_manager.list_tools()}
        assert left == set(gm.tools_for("combat")) and "draft_pawns" in left and "build_room" not in left
    finally:
        for name in names:                                  # вернуть всё, чтобы не ломать другие тесты
            if name not in {t.name for t in m.mcp._tool_manager.list_tools()}:
                m.mcp.add_tool(getattr(m, name), name=name)
