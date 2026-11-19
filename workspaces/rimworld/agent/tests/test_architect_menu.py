"""Остальное меню «Архитектор»: пол, крыша, разрешённые зоны, охота, сбор урожая, перенос. Игры и мода нет: ответы мода подделаны."""
import json

import pytest

import game_modes as gm
import module_manifest
import rimworld_mcp as m
import workspace_module as wm
import workspace_tree as wt


class FakeMod:
    """Поддельный мод: пишет все команды и отвечает заготовкой (строка или функция команды); describe_def отдаёт исследования."""

    def __init__(self):
        self.sent: list[dict] = []
        self.replies: dict = {}
        self.research: list[dict] = []

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd["type"] == "describe_def":
            return json.dumps({"requires": {"research": self.research}})
        reply = self.replies.get(cmd["type"], "{}")
        return reply(cmd) if callable(reply) else reply

    def of(self, kind):
        return [c for c in self.sent if c["type"] == kind]


@pytest.fixture
def mod(monkeypatch):
    fake = FakeMod()
    monkeypatch.setattr(m, "_need_mod", lambda cmd: None)
    monkeypatch.setattr(m, "_query", fake.query)
    return fake


def js(**kw):
    return json.dumps(kw, ensure_ascii=False)


# ── пол ──────────────────────────────────────────────────────────────────────

def test_floor_rectangle_summary_with_reasons_and_materials(mod):
    mod.replies["build_floor"] = js(**{"def": "WoodPlankFloor"}, requested=36, placed=30, skipped=6,
                                    skipped_reasons=[{"reason": "уже такой пол", "count": 4}, {"reason": "вне карты", "count": 2}],
                                    cost={"materials": [{"def": "WoodLog", "need": 120, "have": 80}]},
                                    problems=["не хватает WoodLog: нужно 120, на складе 80."])
    out = m.place_floor("WoodPlankFloor", x1=47, z1=57, x2=40, z2=50)
    assert out.startswith("Пол WoodPlankFloor: поставлено 30, пропущено 6 (уже такой пол: 4, вне карты: 2)")
    assert "WoodLog 120 (на складе 80)" in out and "не хватает WoodLog" in out
    cmd = mod.of("build_floor")[0]
    assert cmd == {"type": "build_floor", "def_name": "WoodPlankFloor", "x1": 47, "z1": 57, "x2": 40, "z2": 50}     # порядок углов решает мод


def test_floor_cells_and_material_are_passed(mod):
    mod.replies["build_floor"] = js(**{"def": "CarpetRed"}, placed=2, skipped=0)
    out = m.place_floor("CarpetRed", cells=[{"x": 1, "z": 2}, {"x": 1, "z": 3}], material="Cloth")
    assert out == "Пол CarpetRed: поставлено 2, пропущено 0."
    assert mod.of("build_floor")[0]["cells"] == [{"x": 1, "z": 2}, {"x": 1, "z": 3}] and mod.of("build_floor")[0]["material"] == "Cloth"
    assert "x1" not in mod.of("build_floor")[0]


@pytest.mark.parametrize("kwargs, expect", [
    ({}, "задай прямоугольник"),
    ({"x1": 1, "z1": 1}, "не хватает: x2, z2"),
    ({"x1": 1, "z1": 1, "x2": 2.5, "z2": 3}, "x2 должен быть целым"),
    ({"x1": 1, "z1": 1, "x2": True, "z2": 3}, "x2 должен быть целым"),
    ({"x1": -1, "z1": 1, "x2": 3, "z2": 3}, "не бывают отрицательными"),
    ({"x1": 0, "z1": 0, "x2": 20, "z2": 20}, "слишком большая: 441"),
    ({"x1": 0, "z1": 0, "x2": 1, "z2": 1, "cells": [{"x": 1, "z": 1}]}, "не оба способа"),
    ({"cells": [{"x": 1}]}, "cells[0]"),
    ({"cells": [{"x": 1, "z": -4}]}, "отрицательными"),
    ({"cells": "40,50"}, "cells должен быть списком"),
    ({"cells": [{"x": i, "z": 0} for i in range(401)]}, "слишком много клеток: 401"),
])
def test_floor_invalid_parameters_do_not_reach_the_mod(mod, kwargs, expect):
    out = m.place_floor("WoodPlankFloor", **kwargs)
    assert out.startswith("ERROR") and expect in out
    assert mod.sent == []


def test_floor_limit_is_exactly_400_cells(mod):
    mod.replies["build_floor"] = js(**{"def": "Concrete"}, placed=400, skipped=0)
    assert m.place_floor("Concrete", x1=0, z1=0, x2=19, z2=19).startswith("Пол Concrete: поставлено 400")
    assert m.place_floor("Concrete", cells=[{"x": i, "z": 0} for i in range(400)]).startswith("Пол Concrete: поставлено 400")


def test_floor_locked_by_research_is_not_sent(mod):
    mod.research = [{"defName": "Stonecutting", "done": False}, {"defName": "Smithing", "done": True}]
    out = m.place_floor("Concrete", x1=0, z1=0, x2=3, z2=3)
    assert out.startswith("ERROR: ЗАКРЫТО ИССЛЕДОВАНИЕМ") and "Stonecutting" in out and "Smithing" not in out and "unlock_for" in out
    assert "пол Concrete" in out
    assert mod.of("build_floor") == []


def test_floor_mod_error_is_returned_as_is(mod):
    mod.replies["build_floor"] = "ERROR: Неизвестный TerrainDef (пол): 'Foo'"
    assert m.place_floor("Foo", x1=0, z1=0, x2=1, z2=1) == "ERROR: Неизвестный TerrainDef (пол): 'Foo'"


def test_old_mod_without_the_command_is_reported(mod, monkeypatch):
    monkeypatch.setattr(m, "_need_mod", lambda cmd: f"ERROR: мод старый, команды {cmd} нет")
    assert m.place_floor("Concrete", x1=0, z1=0, x2=1, z2=1) == "ERROR: мод старый, команды build_floor нет"
    assert m.designate_roof("build", 0, 0, 1, 1) == "ERROR: мод старый, команды designate_roof нет"
    assert m.list_allowed_areas() == "ERROR: мод старый, команды list_allowed_areas нет"
    assert m.search_terrain_defs("tile").endswith("search_terrain_defs нет")
    assert mod.sent == []


def test_terrain_search_and_describe_pass_through(mod):
    mod.replies["search_terrain_defs"] = js(count=1, results=[{"def_name": "TileSandstone"}])
    assert json.loads(m.search_terrain_defs("sand"))["results"][0]["def_name"] == "TileSandstone"
    assert mod.sent[-1] == {"type": "search_terrain_defs", "query": "sand"}
    m.search_terrain_defs()
    assert mod.sent[-1]["query"] == ""                                      # пустой запрос: все полы
    m.describe_terrain_def("WoodPlankFloor", count=64, material="WoodLog")
    assert mod.sent[-1] == {"type": "describe_terrain_def", "def_name": "WoodPlankFloor", "count": 64, "material": "WoodLog"}
    n = len(mod.sent)
    assert m.describe_terrain_def("WoodPlankFloor", count=0).startswith("ERROR: count")
    assert len(mod.sent) == n


# ── крыша ────────────────────────────────────────────────────────────────────

def test_roof_build_and_remove(mod):
    mod.replies["designate_roof"] = lambda cmd: js(mode=cmd["mode"], requested=25, designated=20, skipped=5,
                                                   skipped_reasons=[{"reason": "нет опоры", "count": 5}], note="Область помечена.")
    out = m.designate_roof("build", x1=10, z1=10, x2=14, z2=14)
    assert out == "Крыша (строить): помечено 20, пропущено 5 (нет опоры: 5). Область помечена."
    assert mod.sent[-1] == {"type": "designate_roof", "mode": "build", "x1": 10, "z1": 10, "x2": 14, "z2": 14}
    assert m.designate_roof(" REMOVE ", cells=[{"x": 1, "z": 1}]).startswith("Крыша (убрать)")
    assert mod.sent[-1]["mode"] == "remove" and mod.sent[-1]["cells"] == [{"x": 1, "z": 1}]


def test_roof_default_mode_is_build(mod):
    mod.replies["designate_roof"] = js(designated=1, skipped=0)
    m.designate_roof(x1=1, z1=1, x2=1, z2=1)
    assert mod.sent[-1]["mode"] == "build"


def test_roof_errors_and_limit(mod):
    assert "mode «paint» неизвестен" in m.designate_roof("paint", 0, 0, 1, 1)
    assert m.designate_roof("build").startswith("ERROR: задай прямоугольник")
    assert "слишком большая: 961" in m.designate_roof("build", 0, 0, 30, 30)
    assert m.designate_roof("build", 0, 0, 29, 29) == "{}"                                 # 900 клеток допустимо: команда ушла, ответ мода без сводки отдан как есть
    assert len(mod.of("designate_roof")) == 1


# ── разрешённые зоны ─────────────────────────────────────────────────────────

def test_create_allowed_area_new_and_existing(mod):
    mod.replies["create_allowed_area"] = js(name="Огород", created=True, added=24, already_in_area=1, outside_map=0, total_cells=24)
    out = m.create_allowed_area("  Огород ", x1=30, z1=30, x2=35, z2=34)
    assert out == "Создана зона «Огород»: добавлено 24 клеток, всего 24, уже входили 1."
    assert mod.sent[-1] == {"type": "create_allowed_area", "name": "Огород", "x1": 30, "z1": 30, "x2": 35, "z2": 34}
    mod.replies["create_allowed_area"] = js(name="Огород", created=False, added=3, already_in_area=0, outside_map=2, total_cells=27)
    assert m.create_allowed_area("Огород", cells=[{"x": 1, "z": 1}]).startswith("Зона уже была, клетки добавлены: «Огород»: добавлено 3")


def test_create_allowed_area_validation(mod):
    assert m.create_allowed_area("  ", x1=1, z1=1, x2=2, z2=2).startswith("ERROR: name")
    assert m.create_allowed_area("A").startswith("ERROR: задай прямоугольник")
    assert "слишком большая" in m.create_allowed_area("A", 0, 0, 50, 50)                    # 51*51 = 2601 > 2500
    assert mod.sent == []


def test_delete_and_set_pawn_area(mod):
    mod.replies["delete_allowed_area"] = js(deleted="Огород", cells=24)
    assert m.delete_allowed_area("Огород") == "Зона «Огород» удалена (24 клеток)."
    mod.replies["delete_allowed_area"] = "ERROR: Разрешённой зоны «Х» нет. Есть: Огород."
    assert m.delete_allowed_area("Х").startswith("ERROR: Разрешённой зоны")
    mod.replies["set_pawn_area"] = lambda cmd: js(pawn="Анна", id=cmd["pawn_id"], area=cmd["area_name"] or None, restricted=bool(cmd["area_name"]))
    assert m.set_pawn_area("Human1", "Огород") == "Анна ходит только по зоне «Огород»."
    assert m.set_pawn_area("Human1", "") == "Анна: ограничения зоной нет, ходит где угодно."
    assert mod.sent[-1] == {"type": "set_pawn_area", "pawn_id": "Human1", "area_name": ""}
    assert m.set_pawn_area("").startswith("ERROR: pawn_id") and m.delete_allowed_area(" ").startswith("ERROR: name")


def test_list_allowed_areas_is_passed_through(mod):
    mod.replies["list_allowed_areas"] = js(count=1, can_create_more=True, areas=[{"name": "Огород", "cells": 24}])
    assert json.loads(m.list_allowed_areas())["areas"][0]["name"] == "Огород"
    assert mod.sent[-1] == {"type": "list_allowed_areas"}


# ── охота, сбор урожая, перенос ──────────────────────────────────────────────

def test_hunt_collects_ids_without_duplicates(mod):
    mod.replies["designate_hunt"] = js(designated=2, skipped_count=1, skipped=[{"id": "Deer3", "reason": "не дикое"}], warning="нет охотника")
    out = m.designate_hunt(animal_id="Deer1, Deer2", animal_ids=["Deer2", "Deer3"])
    assert mod.sent[-1] == {"type": "designate_hunt", "animal_ids": ["Deer2", "Deer3", "Deer1"]}
    assert out == "Охота назначена на 2, пропущено 1 (Deer3: не дикое). Внимание: нет охотника."


def test_hunt_validation(mod):
    assert m.designate_hunt().startswith("ERROR: задай animal_id")
    assert m.designate_hunt(animal_ids="Deer1").startswith("ERROR: animal_ids должен быть списком")
    assert "слишком много целей: 201" in m.designate_hunt(animal_ids=[f"A{i}" for i in range(201)])
    assert mod.sent == []


def test_harvest_modes_and_targets(mod):
    mod.replies["designate_harvest"] = lambda cmd: js(mode=cmd["mode"], requested=10, designated=7, skipped=3,
                                                      skipped_reasons=[{"reason": "ещё не созрело", "count": 3}])
    out = m.designate_harvest(x1=30, z1=30, x2=33, z2=32)
    assert out == "Сбор урожая: назначено 7, пропущено 3 (ещё не созрело: 3)."
    assert mod.sent[-1] == {"type": "designate_harvest", "mode": "harvest", "x1": 30, "z1": 30, "x2": 33, "z2": 32}
    assert m.designate_harvest("cut", cells=[{"x": 5, "z": 6}]).startswith("Срезка: назначено 7")
    assert mod.sent[-1]["mode"] == "cut" and mod.sent[-1]["cells"] == [{"x": 5, "z": 6}]
    m.designate_harvest(plant_ids=["Rice1", "Rice2"])
    assert mod.sent[-1] == {"type": "designate_harvest", "mode": "harvest", "plant_ids": ["Rice1", "Rice2"]}


def test_harvest_validation(mod):
    assert "mode «pick» неизвестен" in m.designate_harvest("pick", cells=[{"x": 1, "z": 1}])
    out = m.designate_harvest()
    assert out.startswith("ERROR: задай plant_ids, прямоугольник")
    assert "либо plant_ids, либо клетки" in m.designate_harvest(plant_ids=["P1"], cells=[{"x": 1, "z": 1}])
    assert "не больше 1600" in m.designate_harvest(x1=0, z1=0, x2=40, z2=40)               # 41*41 = 1681
    assert mod.sent == []


def test_haul(mod):
    mod.replies["designate_haul"] = js(designated=1, skipped_count=2, skipped=[{"id": "W1", "reason": "не переносят"}, {"id": "W2", "reason": "не найдено"}])
    assert m.designate_haul(["Steel1", "W1", "W2", "Steel1"]) == "Перенос назначен на 1, пропущено 2 (W1: не переносят; W2: не найдено)."
    assert mod.sent[-1] == {"type": "designate_haul", "thing_ids": ["Steel1", "W1", "W2"]}
    assert m.designate_haul([]).startswith("ERROR: задай thing_id")
    assert "слишком много целей" in m.designate_haul([f"T{i}" for i in range(201)])


# ── дерево, манифест, режимы ─────────────────────────────────────────────────

NEW_TOOLS = {
    "place_floor": ("architect_floor_place", "architect/floor"),
    "designate_roof": ("architect_zone_roof", "architect/zone"),
    "create_allowed_area": ("architect_zone_allowed_area", "architect/zone"),
    "delete_allowed_area": ("architect_zone_delete_allowed_area", "architect/zone"),
    "set_pawn_area": ("pawn_orders_set_area", "pawn/orders"),
    "designate_hunt": ("architect_gather_hunt", "architect/gather"),
    "designate_harvest": ("architect_gather_harvest", "architect/gather"),
    "designate_haul": ("architect_gather_haul", "architect/gather"),
}
NEW_QUERIES = {
    "search_terrain_defs": "architect_defs_search_terrain",
    "describe_terrain_def": "architect_defs_describe_terrain",
    "list_allowed_areas": "architect_areas",
}


def test_new_tools_are_placed_in_the_tree_and_nodes_stay_within_the_limit():
    for old, (name, node) in NEW_TOOLS.items():
        kind, path, new = wt.TOOLS[old]
        assert kind == "action" and path == node and wm.tool_name(kind, path, new) == name
    for old, name in NEW_QUERIES.items():
        kind, path, new = wt.TOOLS[old]
        assert kind == "query" and wm.tool_name(kind, path, new) == name
    by_node = wt.actions_by_node()
    assert all(len(names) <= wt.MAX_ACTIONS for names in by_node.values())
    assert set(by_node["architect/gather"]) == {"hunt", "harvest", "haul"} and by_node["architect/floor"] == ["place"]
    assert len(by_node["architect/orders"]) <= wt.MAX_ACTIONS                      # приказы охоты и сбора вынесены в соседний узел


def test_new_nodes_open_the_matching_architect_category():
    assert wm.ARCHITECT_CATEGORIES["architect/floor"] == "Floors"
    assert wm.ARCHITECT_CATEGORIES["architect/gather"] == "Orders"
    assert {n.path for n in wt.NODES} >= {"architect/floor", "architect/gather"}


async def test_manifest_has_new_tools_with_node_read_only_and_short_descriptions():
    manifest = await module_manifest.build()
    tools = {t["name"]: t for t in manifest["tools"]}
    for name, node in NEW_TOOLS.values():
        assert tools[name]["node"] == node and not tools[name].get("read_only")
    for name in NEW_QUERIES.values():
        assert tools[name].get("read_only") is True and "node" not in tools[name]
    for name in [n for n, _ in NEW_TOOLS.values()] + list(NEW_QUERIES.values()):
        assert 20 < len(tools[name]["description"]) <= 300, name
    assert {"пол", "плитка", "ковёр", "покрытие", "floor"} <= set(tools["architect_floor_place"]["keywords"])
    assert {"пол", "покрытие", "floor"} <= set(tools["architect_defs_search_terrain"]["keywords"])
    assert "крыша" in tools["architect_zone_roof"]["keywords"] and "охота" in tools["architect_gather_hunt"]["keywords"]
    params = tools["architect_floor_place"]["params"]
    assert params["def_name"]["required"] and not params["cells"]["required"] and params["cells"]["type"] == "array"
    assert '"x": 40' in params["cells"]["description"]
    assert '"Steel101"' in tools["architect_gather_haul"]["params"]["thing_ids"]["description"]


def test_new_tools_belong_to_modes():
    assert {"place_floor", "designate_roof", "search_terrain_defs", "describe_terrain_def"} <= set(gm.MODES["build"].tools)
    assert {"designate_hunt", "designate_harvest", "designate_haul"} <= set(gm.MODES["economy"].tools)
    assert {"create_allowed_area", "delete_allowed_area", "set_pawn_area", "list_allowed_areas"} <= set(gm.MODES["people"].tools)


def test_room_tools_hint_at_the_roof():
    assert "designate_roof" in (m.room_info.__doc__ or "").splitlines()[0]
    assert "designate_roof" in (m.build_room.__doc__ or "").splitlines()[0]
