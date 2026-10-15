"""ТЗ 11e, POL-2/POL-5/POL-6 на уровне инструментов MCP: компактные запросы мира (с лимитом размера), просьба о политике,
алиас switch_mode, правило наблюдения через инструмент, поля клетки у set_task. Состояние игры подменено."""
import json

import policy_manager
import rimworld_mcp as m
import watch_rules


def colony(n_pawns=3):
    pawns = [{"id": f"p{i}", "name": f"Колонист {i}", "current_job": "Wait", "mood_pct": 0.5, "health_pct": 1.0,
              "is_downed": False, "is_drafted": False, "skills": {"Shooting": 5}, "schedule": ["x"] * 24}
             for i in range(n_pawns)]
    return {"tick": 1000, "colony": {"pawns": pawns, "resources": {"Steel": 500, "WoodLog": 80, "Rice": 3},
                                     "food_days": 2.5, "buildings": [{"def": "Wall"}] * 4,
                                     "pending_construction": [{"def": "Wall"}],
                                     "research": {"current": "Орошение", "progress": 0.4, "completed": ["Стрельба"]}}}


def test_query_tools_are_compact_and_bounded_by_colony_size(monkeypatch):
    monkeypatch.setattr(m, "_load_state", lambda: colony(3))
    small = m.get_colonists()
    monkeypatch.setattr(m, "_load_state", lambda: colony(300))
    big = m.get_colonists()
    assert len(big) <= m.QUERY_MAX_CHARS and len(small) < len(big) + 1 and "skills" not in big and "schedule" not in big
    assert json.loads(small)["count"] == 3


def test_resources_research_buildings_return_small_json(monkeypatch):
    monkeypatch.setattr(m, "_load_state", lambda: colony())
    res = json.loads(m.get_resources())
    assert res["food_days"] == 2.5 and res["resources"]["Steel"] == 500
    assert json.loads(m.get_resources(min_amount=100))["resources"] == {"Steel": 500}     # мелочь (80, 3) отсеяна
    rs = json.loads(m.get_research())
    assert rs["current"] == "Орошение" and rs["completed_count"] == 1
    b = json.loads(m.get_buildings("wall"))
    assert b["built"] == {"Wall": 4} and b["pending"] == {"Wall": 1}


def test_query_tools_report_missing_game(monkeypatch):
    monkeypatch.setattr(m, "_load_state", lambda: None)
    assert m.get_colonists().startswith("ERROR") and m.get_research().startswith("ERROR")


def test_switch_policy_writes_a_request_the_controller_takes(tmp_path, monkeypatch):
    monkeypatch.setattr(policy_manager, "REQUEST", tmp_path / "req.json")
    assert m.switch_policy("планирование", "разметить комнату").startswith("OK")
    assert policy_manager.take_request()["policy"] == "plan"
    assert m.switch_policy("combat", "хочу в бой").startswith("ERROR")          # бой только событиями игры
    assert m.switch_policy("dance").startswith("ERROR")


def test_switch_mode_alias_moves_policy_for_policy_names_only(tmp_path, monkeypatch):
    import game_modes
    monkeypatch.setattr(policy_manager, "REQUEST", tmp_path / "req.json")
    monkeypatch.setattr(game_modes, "_REQUEST", tmp_path / "mode_req.json")
    assert m.switch_mode("plan", "планирую").startswith("OK")                   # имя политики: переход политики
    assert policy_manager.take_request()["policy"] == "plan"
    assert m.switch_mode("build", "стройка").startswith("OK")                   # имя режима инструментов: как раньше
    assert game_modes.take_request()["mode"] == "build"


def test_watch_rule_tool_validates_stores_and_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(m, "_load_state", lambda: {"tick": 5000})
    ok = m.set_watch_rule("research_done", "жду орошение", field="research", value="Орошение", switch_to="plan", ttl_h=2)
    assert ok.startswith("OK")
    stored = json.loads((tmp_path / "watch.json").read_text(encoding="utf-8"))["rules"]
    assert len(stored) == 1 and stored[0]["until_tick"] == 5000 + 2 * watch_rules.TICKS_PER_HOUR
    assert m.set_watch_rule("weather", "зачем").startswith("ERROR")               # вид не из каталога
    assert m.set_watch_rule("raid", "проверка", switch_to="combat").startswith("ERROR")
    rid = stored[0]["id"]
    assert m.clear_watch_rule(rid).startswith("OK") and m.clear_watch_rule(rid).startswith("ERROR")


def test_watch_rule_needs_a_running_game(tmp_path, monkeypatch):
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(m, "_load_state", lambda: None)
    assert m.set_watch_rule("raid", "жду набег").startswith("ERROR")


def test_set_task_stores_cell_work_and_pawn(tmp_path, monkeypatch):
    path = tmp_path / "tasks.json"
    monkeypatch.setattr(m, "_TASKS_FILE", path)
    assert m.set_task("дерево у реки", x=5, z=6, work="PlantCutting", pawn_id="p1").startswith("OK")
    task = json.loads(path.read_text(encoding="utf-8"))[0]
    assert task["x"] == 5 and task["z"] == 6 and task["work"] == "PlantCutting" and task["pawn_id"] == "p1"
    m.set_task("дерево у реки", status="in_progress")                           # без клетки: старая клетка сохраняется
    assert json.loads(path.read_text(encoding="utf-8"))[0]["x"] == 5
