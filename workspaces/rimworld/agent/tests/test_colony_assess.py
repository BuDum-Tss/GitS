"""Оценка колонии (colony_assess): пробелы, пороги, порядок серьёзности, пустое состояние; без игры и сети."""
import json

import colony_assess as ca


def pawn(i, mood=0.7, job="Research", work=None, equipped=None, thoughts=None, **kw):
    p = {"id": f"P{i}", "name": f"Кол{i}", "mood_pct": mood, "current_job": job, "equipped": equipped,
         "work_priorities": work if work is not None else {"Construction": 3, "Hauling": 3, "Growing": 3},
         "mood_thoughts": thoughts or [], "is_prisoner": False, "is_downed": False, "is_drafted": False}
    p.update(kw)
    return p


def bld(name, x=0, z=0):
    return {"id": f"{name}{x}{z}", "def": name, "position": {"x": x, "z": z}}


def healthy(n=3):
    """Благополучная колония: помещение, кровати, стол, свет, склад, поле, оборона, исследование."""
    pawns = [pawn(i, equipped="Gun_Revolver") for i in range(n)]
    blds = [bld("Bed", i, 1) for i in range(n)] + [bld("Door", 5, 5), bld("Table2x2c", 3, 3), bld("DiningChair", 3, 4),
            bld("StandingLamp", 2, 2), bld("FueledStove", 4, 4), bld("SimpleResearchBench", 6, 6), bld("Turret_MiniTurret", 9, 9)]
    blds += [bld("Wall", i, 0) for i in range(8)]
    return {"tick": 1, "manual_priorities": True, "threats": [],
            "colony": {"pawns": pawns, "buildings": blds, "resources": {"WoodLog": 200, "Steel": 100, "MealSimple": 30},
                       "zones": [{"type": "Zone_Stockpile"}, {"type": "Zone_Growing"}], "food_days": 12.0,
                       "pending_construction": [], "research": {"current": "Smithing", "available": ["A", "B"], "completed": ["X"]}}}


def good_rooms(state):
    return {f"{b['position']['x']},{b['position']['z']}": {"room": True, "is_proper_room": True, "roofed": True, "role": "спальня", "cells": 20}
            for b in state["colony"]["buildings"] if b["def"] == "Bed"}


def ids(res):
    return [g["id"] for g in res["gaps"]]


def test_empty_state_gives_no_gaps_and_not_ok():
    for state in (None, {}, {"colony": {}}, {"colony": {"pawns": []}}, {"colony": {"pawns": [pawn(1, is_prisoner=True)]}}):
        res = ca.assess(state)
        assert res["ok"] is False and res["gaps"] == []
    assert ca.top_gaps_text(ca.assess({})) == "оценка состояния недоступна"


def test_healthy_colony_has_no_serious_gaps():
    state = healthy()
    res = ca.assess(state, rooms=good_rooms(state), history={"raids": 0, "repelled": 0, "deaths": 0})
    assert res["ok"] and res["gaps"] == []
    assert res["shelter"]["proper_rooms"] == 1 and res["shelter"]["beds_in_room"] == 3
    assert res["food"]["days"] == 12.0 and res["storage"]["has_zone"] and res["colonists"]["total"] == 3
    assert ca.top_gaps_text(res) == "заметных пробелов в оценке нет"
    json.dumps(res, ensure_ascii=False)                                   # ответ инструмента сериализуется


def test_people_counts_working_idle_downed_resting_and_prisoners_are_excluded():
    state = healthy(5)
    p = state["colony"]["pawns"]
    p[0]["current_job"] = "Wait_Wander"
    p[1]["current_job"] = "GotoWander"
    p[2]["is_downed"] = True
    p[3]["current_job"] = "LayDown"
    state["colony"]["pawns"].append(pawn(9, is_prisoner=True, job="Wait_Wander"))
    c = ca.assess(state)["colonists"]
    assert (c["total"], c["working"], c["idle"], c["downed"], c["resting"]) == (5, 1, 2, 1, 1)
    assert c["working_share"] == 0.25 and c["idle_names"] == ["Кол0", "Кол1"]
    gaps = {g["id"]: g for g in ca.assess(state)["gaps"]}
    assert gaps["idle"]["severity"] == 2 and "простаивают 2 из 5" in gaps["idle"]["fact"]
    assert gaps["downed"]["severity"] == 4


def test_beds_gap_thresholds_and_double_bed_counts_two():
    state = healthy(4)
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if b["def"] != "Bed"] + [bld("Bed", 1, 1)]
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["beds"]["severity"] == 4 and g["beds"]["fact"] == "кроватей 1 при 4 колонистах"
    state["colony"]["buildings"] += [bld("Bed", 2, 2), bld("DoubleBed", 3, 2)]          # 4 места для сна
    assert "beds" not in ids(ca.assess(state))
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if b["def"] != "DoubleBed"]       # 2 места из 4: половина, не меньше
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["beds"]["severity"] == 3
    state["colony"]["buildings"] += [bld("Bed", 4, 4)]                                                          # 3 места из 4
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["beds"]["severity"] == 3
    state["colony"]["buildings"] = [bld("Bed", 1, 1)]                                                           # 1 место из 4: меньше половины
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["beds"]["severity"] == 4


def test_room_unknown_without_probes_and_known_with_probes():
    state = healthy(3)
    assert "no_room" not in ids(ca.assess(state))                                 # без room_info помещение не проверено, пробела нет
    assert ca.assess(state)["shelter"]["known"] is False
    open_air = {k: {"room": True, "is_proper_room": False, "roofed": False} for k in good_rooms(state)}
    gap = {x["id"]: x for x in ca.assess(state, rooms=open_air)["gaps"]}["no_room"]
    assert gap["severity"] == 4 and "замкнутого, с дверью и крышей" in gap["fact"]
    no_roof = {k: {"room": True, "is_proper_room": True, "roofed": False} for k in good_rooms(state)}
    res = ca.assess(state, rooms=no_roof)
    assert "no_room" in ids(res) and res["shelter"]["unroofed"] == 3 and "без крыши 3" in {x["id"]: x for x in res["gaps"]}["no_room"]["fact"]
    some = good_rooms(state)
    first = next(iter(some))
    some[first] = {"room": True, "is_proper_room": False, "roofed": True}
    res = ca.assess(state, rooms=some)
    assert "no_room" not in ids(res) and {x["id"]: x for x in res["gaps"]}["beds_outside"]["fact"] == "в помещении кроватей 2 из 3, колонистов 3"


def test_no_door_means_no_proper_room_even_if_mod_says_so():
    state = healthy(2)
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if b["def"] != "Door"]
    assert "no_room" in ids(ca.assess(state, rooms=good_rooms(state)))


def test_no_walls_and_doors_at_all_is_a_gap_even_without_probes():
    state = healthy(2)
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if b["def"] not in ("Wall", "Door")]
    assert "no_room" in ids(ca.assess(state))


def test_food_thresholds():
    state = healthy()
    for days, expected in ((1.0, ("food_critical", 5)), (5.0, ("food_low", 4)), (9.0, None), (12.0, None)):
        state["colony"]["food_days"] = days
        found = {g["id"]: g for g in ca.assess(state)["gaps"]}
        if expected:
            assert found[expected[0]]["severity"] == expected[1] and "еды на" in found[expected[0]]["fact"]
        else:
            assert not {"food_critical", "food_low"} & set(found)
    state["colony"]["zones"] = [{"type": "Zone_Stockpile"}]                       # поля нет
    state["colony"]["food_days"] = 3.0
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["no_field"]["severity"] == 4 and g["no_field"]["fact"] == "поля нет, еды на 3 дня"
    state["colony"]["food_days"] = 30.0
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["no_field"]["severity"] == 2


def test_no_stockpile_and_few_materials():
    state = healthy()
    state["colony"]["zones"] = [{"type": "Zone_Growing"}]
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["no_stockpile"]["severity"] == 4 and "зоны склада нет" in g["no_stockpile"]["fact"]
    state["colony"]["zones"] = [{"type": "Zone_Stockpile"}, {"type": "Zone_Growing"}]
    state["colony"]["resources"] = {"WoodLog": 5, "MealSimple": 10}
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["few_materials"]["severity"] == 3 and "5" in g["few_materials"]["fact"] and "no_stockpile" not in g


def test_table_chairs_light_gaps():
    state = healthy()
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if b["def"] not in ("Table2x2c", "DiningChair", "StandingLamp")]
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["no_table"]["severity"] == 3 and g["no_light"]["severity"] == 2 and "no_chairs" not in g
    state["colony"]["buildings"].append(bld("Table2x2c"))
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert "no_table" not in g and g["no_chairs"]["severity"] == 2


def test_mood_thresholds_causes_and_counts():
    state = healthy(4)
    pawns = state["colony"]["pawns"]
    pawns[0].update(mood_pct=0.39, mood_thoughts=[{"label": "Спал на земле", "effect": -4.0}, {"label": "Хорошая еда", "effect": 3.0}])
    pawns[1].update(mood_pct=0.5, mood_thoughts=[{"label": "Спал на земле", "effect": -4.0}, {"label": "Грязно", "effect": -2.0}])
    res = ca.assess(state)
    mood = res["mood"]
    assert mood["low_count"] == 1 and mood["min"] == 0.39 and mood["low_threshold"] == 0.4
    assert mood["causes"][0] == {"label": "Спал на земле", "effect": -8.0, "colonists": 2}
    gap = {x["id"]: x for x in res["gaps"]}["low_mood"]
    assert gap["severity"] == 3 and "ниже 0,4: 1 из 4" in gap["fact"] and "Спал на земле" in gap["fact"]
    pawns[0]["mood_pct"] = 0.3                                   # ниже порога срыва
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["low_mood"]["severity"] == 4
    pawns[0]["mood_pct"] = 0.1                                   # серьёзный срыв
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["low_mood"]["severity"] == 5
    pawns[0]["mood_pct"] = 0.4                                   # ровно порог: не ниже
    assert "low_mood" not in ids(ca.assess(state))


def test_work_priorities_gap_needs_enough_colonists_with_each_work():
    state = healthy(6)                                      # нужно хотя бы 2 на каждую работу
    for p in state["colony"]["pawns"]:
        p["work_priorities"] = {"Research": 3}
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}["work_priorities"]
    assert g["severity"] == 4 and "Строительство у 0 из 6" in g["fact"] and "нужно хотя бы 2" in g["fact"]
    for p in state["colony"]["pawns"][:2]:
        p["work_priorities"] = {"Construction": 3, "Hauling": 3, "Growing": 3}
    assert "work_priorities" not in ids(ca.assess(state))
    state["colony"]["pawns"][0]["work_priorities"] = {"Construction": 3, "Hauling": 3}               # Growing у одного
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}["work_priorities"]
    assert g["severity"] == 3 and "Выращивание у 1 из 6" in g["fact"]
    del state["colony"]["pawns"][0]["work_priorities"]                                               # поле не отдано: молчим
    assert "work_priorities" not in ids(ca.assess(state)) and ca.assess(state)["work"]["known"] is False


def test_defense_gaps_and_history():
    state = healthy(4)
    for p in state["colony"]["pawns"]:
        p["equipped"] = None
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if "Turret" not in b["def"]]
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["unarmed"]["severity"] == 3 and g["unarmed"]["fact"] == "вооружены 0 из 4"
    assert g["no_defense"]["severity"] == 2 and "турелей 0, мешков с песком 0, ловушек 0" in g["no_defense"]["fact"]
    g = {x["id"]: x for x in ca.assess(state, history={"raids": 2, "repelled": 1, "deaths": 1})["gaps"]}
    assert g["unarmed"]["severity"] == 4 and "атак было 2, отбито 1, потеряно людей 1" in g["no_defense"]["fact"]
    state["colony"]["buildings"] += [bld("Sandbags"), bld("TrapSpike")]
    res = ca.assess(state, history={"raids": 3, "repelled": 2, "deaths": 2})
    assert "no_defense" not in ids(res) and {x["id"]: x for x in res["gaps"]}["losses"]["severity"] == 4
    assert res["defense"]["sandbags"] == 1 and res["defense"]["traps"] == 1 and res["defense"]["raids"] == 3


def test_threat_now_is_top_gap():
    state = healthy()
    state["threats"] = [{"id": "T1"}, {"id": "T2"}]
    res = ca.assess(state)
    assert res["gaps"][0] == {"id": "threat_now", "severity": 5, "fact": "на карте враги: 2"}


def test_research_gaps_and_pending():
    state = healthy()
    state["colony"]["research"] = {"current": None, "available": ["A", "B", "C"], "completed": []}
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}
    assert g["no_research"]["severity"] == 3 and "доступно проектов 3" in g["no_research"]["fact"]
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if "Research" not in b["def"]]
    assert "no_research_bench" in ids(ca.assess(state)) and "no_research" not in ids(ca.assess(state))
    state["colony"]["pending_construction"] = [{"def": "Wall", "status": "blueprint"}] * 3
    g = {x["id"]: x for x in ca.assess(state)["gaps"]}["unfinished"]
    assert g["severity"] == 2 and g["fact"] == "недостроено 3: Wall x3"
    state["colony"]["pending_construction"] = [{"def": "Wall"}] * 5 + [{"def": "Door"}] * 2
    assert {x["id"]: x for x in ca.assess(state)["gaps"]}["unfinished"]["severity"] == 3


def test_stove_gap_only_when_there_is_something_to_cook():
    state = healthy()
    state["colony"]["buildings"] = [b for b in state["colony"]["buildings"] if "Stove" not in b["def"]]
    assert "no_stove" in ids(ca.assess(state))
    state["colony"]["zones"] = [{"type": "Zone_Stockpile"}]
    state["colony"]["resources"] = {"WoodLog": 100, "Steel": 100}
    assert "no_stove" not in ids(ca.assess(state))                                 # нечего готовить и поля нет


def test_gaps_sorted_by_severity_and_evidence_is_a_fact_not_an_order():
    state = healthy(4)
    state["colony"]["food_days"] = 2.0
    state["colony"]["zones"] = []
    state["colony"]["buildings"] = []
    state["colony"]["pawns"][0].update(mood_pct=0.1)
    res = ca.assess(state)
    sev = [g["severity"] for g in res["gaps"]]
    assert sev == sorted(sev, reverse=True) and sev[0] == 5 and all(1 <= s <= 5 for s in sev)
    assert {"food_critical", "no_stockpile", "beds", "no_room", "low_mood", "no_table"} <= set(ids(res))
    for g in res["gaps"]:
        assert set(g) == {"id", "severity", "fact"} and g["fact"]
        assert not any(w in g["fact"].lower() for w in ("сделай", "построй", "поставь", "должна", "нужно построить"))
    text = ca.top_gaps_text(res, top=3)
    assert text.count("; ") >= 2 and text.split("; ")[0] == res["gaps"][0]["fact"]
