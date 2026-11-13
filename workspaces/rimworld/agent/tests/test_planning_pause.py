"""Планирование комнаты: контур прямоугольника, проверка двери, маркер паузы."""
import planning_pause as pp


def test_rect_perimeter_counts():
    cells = pp.rect_perimeter(0, 0, 4, 3)               # 5 x 4: контур 2*(5+4)-4 = 14 клеток
    assert len(cells) == 14 and {"x": 0, "z": 0} in cells and {"x": 2, "z": 1} not in cells
    assert pp.rect_perimeter(4, 3, 0, 0) == pp.rect_perimeter(0, 0, 4, 3) or len(pp.rect_perimeter(4, 3, 0, 0)) == 14


def test_rect_room_door_and_inside():
    walls, doors, inside, err = pp.rect_room(10, 10, 14, 14, 12, 10)
    assert err is None and doors == [{"x": 12, "z": 10}] and {"x": 12, "z": 10} not in walls
    assert len(walls) == 15 and inside == {"x": 12, "z": 12}


def test_rect_room_rejects_bad_door_and_tiny_room():
    assert pp.rect_room(0, 0, 4, 4, 2, 2)[3]            # дверь внутри
    assert pp.rect_room(0, 0, 4, 4, 0, 0)[3]            # дверь в углу
    assert pp.rect_room(0, 0, 1, 4, 0, 2)[3]            # комната без внутренней клетки


def test_pause_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "_MARK", tmp_path / "planner_paused.json")
    assert not pp.marked()
    pp.mark()
    assert pp.marked()
    pp.clear()
    assert not pp.marked()
    pp.clear()                                           # повторная очистка не падает


def test_set_work_priorities_validates_and_reports(monkeypatch):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    import rimworld_mcp as m
    sent = []
    monkeypatch.setattr(m, "_post", lambda c: sent.append(c) or "OK: x")
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "_load_state", lambda: {"colony": {"pawns": [{"id": "p1", "name": "Рика", "work_priorities": {"Cooking": 1, "Hauling": 3}}]}})
    assert "вне 0..4" in m.set_work_priorities("p1", {"Cooking": 9})
    assert m.set_work_priorities("p1", {}).startswith("ERROR")
    ok = m.set_work_priorities("p1", {"Cooking": 1, "Hauling": 3})
    assert ok.startswith("OK") and len(sent) == 2
    bad = m.set_work_priorities("p1", {"Cooking": 2})
    assert bad.startswith("ERROR") and "Cooking" in bad
    monkeypatch.setattr(m, "_post", lambda c: "ERROR: Окно «Work» закрыто")
    assert "закрыто" in m.set_work_priorities("p1", {"Cooking": 1})


def test_schedule_ranges_and_plan(monkeypatch):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    import rimworld_mcp as m
    night = ["Sleep"] * 6 + ["Anything"] * 2 + ["Work"] * 9 + ["Joy"] * 4 + ["Sleep"] * 3          # сон 21-5, через полночь
    r = m._ranges(night)
    assert {"from": 21, "to": 5, "type": "Sleep"} in r and {"from": 8, "to": 16, "type": "Work"} in r and len(r) == 4
    assert m._ranges(["Work"] * 24) == [{"from": 0, "to": 23, "type": "Work"}] and m._ranges([]) == []
    state = {"colony": {"pawns": [{"id": "p1", "name": "Рика", "schedule": list(night)}]}}
    posted = []
    monkeypatch.setattr(m, "_load_state", lambda: state)

    def fake_post(c):
        posted.append((c["hour"], c["schedule_type"]))
        state["colony"]["pawns"][0]["schedule"][c["hour"]] = c["schedule_type"]
        return "OK"
    monkeypatch.setattr(m, "_post", fake_post)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    assert m.set_schedule_plan("p1", []).startswith("ERROR") and "Meditate" in m.set_schedule_plan("p1", [{"from": 1, "to": 2, "type": "Nap"}])
    out = m.set_schedule_plan("p1", [{"from": 22, "to": 5, "type": "Sleep"}, {"from": 18, "to": 21, "type": "Joy"}])
    assert out.startswith("OK") and (23, "Sleep") in posted and (0, "Sleep") in posted and (18, "Joy") in posted
    table = __import__("json").loads(m.get_schedule_table())
    assert table["pawns"][0]["name"] == "Рика" and table["pawns"][0]["ranges"]
