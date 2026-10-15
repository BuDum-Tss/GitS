"""Разница между снимками состояния игры: что изменилось и что важно."""
import copy

import statediff as sd


def state(tick=1000, paused=False, hp=1.0, downed=False, threats=None, buildings=None):
    return {
        "tick": tick, "save_id": "s1", "paused": paused, "time_speed": 1, "weather": "Clear", "season": "Spring",
        "colony": {
            "pawns": [{"id": "p1", "name": "Ли", "health_pct": hp, "mood_pct": 0.6, "current_job": "Wait",
                       "is_drafted": False, "is_downed": downed, "bleeding": 0}],
            "buildings": buildings or [], "resources": {"WoodLog": 100}, "pending_construction": [],
            "research": {"current": None, "completed": []}, "food_days": 8.0, "alerts": [], "visitors": [],
        },
        "threats": threats or [],
    }


def texts(changes):
    return [t for _, t in changes]


def test_no_change_gives_nothing():
    a = sd.snapshot(state())
    assert sd.compare(a, copy.deepcopy(a)) == []


def test_pause_threat_and_downed_are_important():
    a = sd.snapshot(state())
    b = sd.snapshot(state(tick=1100, paused=True, downed=True, hp=0.4,
                          threats=[{"id": "t1", "name": "жук", "kind": "animal", "health_pct": 1.0}]))
    changes = sd.compare(a, b)
    assert all(imp for imp, _ in changes[:3])
    joined = " | ".join(texts(changes))
    assert "пауз" in joined and "жук" in joined and "упал" in joined and "здоровье" in joined


def test_new_building_and_resource_move():
    a = sd.snapshot(state())
    s = state(tick=1200, buildings=[{"def": "Bed"}])
    s["colony"]["resources"]["WoodLog"] = 10
    changes = sd.compare(a, sd.snapshot(s))
    joined = " | ".join(texts(changes))
    assert "построено: Bed" in joined and "WoodLog: 100" in joined


def test_other_save_or_rewind_is_ignored():
    a = sd.snapshot(state(tick=5000))
    assert sd.compare(a, sd.snapshot(state(tick=100))) == []
    other = state(tick=6000)
    other["save_id"] = "s2"
    assert sd.compare(a, sd.snapshot(other)) == []


def test_render_limits_lines():
    changes = [(False, f"изменение {i}") for i in range(40)]
    lines = sd.render(changes, limit=25)
    assert len(lines) == 26 and lines[-1].startswith("…и ещё 15")


def test_idle_pawns_are_listed_but_not_fighters_or_downed():
    st = state()
    st["colony"]["pawns"] = [
        {"id": "a", "name": "Ли", "current_job": "GotoWander"}, {"id": "b", "name": "Фелл", "current_job": "Construct"},
        {"id": "c", "name": "Бун", "current_job": "Wait_Wander", "is_drafted": True},
        {"id": "d", "name": "Сэм", "current_job": "Wait_Wander", "is_downed": True}, {"id": "e", "name": "Тим", "current_job": "Wait"}]
    assert sd.idle_pawns(sd.snapshot(st)) == ["Ли", "Тим"]


def test_dismantled_walls_are_not_reported_as_losses():
    walls = [{"def": "Wall"}] * 3
    a, b = state(buildings=walls), state(buildings=walls[:1])
    b["colony"]["pawns"][0]["current_job"] = "Deconstruct"
    changes = sd.compare(sd.snapshot(a), sd.snapshot(b))
    assert any("разобрали Wall ×2" in t for _, t in changes) and not any(imp for imp, t in changes if "Wall" in t)
    c = state(buildings=walls[:1])                       # то же без разборки: настоящая потеря
    assert any(imp and "потеряно или снесено: Wall ×2" in t for imp, t in sd.compare(sd.snapshot(a), sd.snapshot(c)))


def test_built_from_own_blueprint_is_marked():
    a = state()
    a["colony"]["pending_construction"] = [{"def": "Bed"}]
    b = state(buildings=[{"def": "Bed"}])
    assert any("достроен твой чертёж" in t for _, t in sd.compare(sd.snapshot(a), sd.snapshot(b)))


def test_hp_loss_in_a_fight_names_the_fight():
    a = state(hp=1.0)
    b = state(hp=0.7)
    b["colony"]["pawns"][0]["current_job"] = "AttackMelee"
    texts_ = [t for _, t in sd.compare(sd.snapshot(a), sd.snapshot(b))]
    assert any("дерётся" in t and "врагов нет" in t for t in texts_)
    assert any("здоровье 100% → 70%, только что дерётся" in t for t in texts_)
