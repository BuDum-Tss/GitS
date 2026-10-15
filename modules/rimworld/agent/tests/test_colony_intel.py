"""Положение колонии для подсказки: возраст, новое ли поселение, обзор карты по save_id."""
import colony_intel as ci


def _state(tick, buildings=1, pawns=3, save="s1"):
    return {"tick": tick, "save_id": save, "colony": {"pawns": [{}] * pawns, "buildings": [{}] * buildings}}


def test_new_colony_block_and_old_colony_block():
    new = ci.age_block(_state(12000))
    assert "НОВОЕ ПОСЕЛЕНИЕ" in new and "день 0.2" in new.lower().replace("идёт ", "")
    old = ci.age_block(_state(60000 * 40, buildings=30))
    assert "НОВОЕ ПОСЕЛЕНИЕ" not in old and "день 40.0" in old.lower().replace("идёт ", "")
    assert ci.age_block({}) == ""


def test_survey_is_stored_per_save(tmp_path, monkeypatch):
    monkeypatch.setattr(ci, "_FILE", tmp_path / "map_survey.json")
    assert ci.load_survey("s1") is None
    ci.save_survey("s1", {"map": {"w": 250, "h": 250, "biome": "пустыня"}, "colony_center": {"x": 130, "z": 130},
                          "water": [{"x": 95, "z": 60, "cells": 70, "dist": 25}], "ruins": [{"x": 150, "z": 140, "cells": 9, "dist": 14}],
                          "ores": {"MineableSteel": [{"x": 80, "z": 200, "cells": 5, "dist": 60}]}})
    assert ci.load_survey("s2") is None                      # чужая колония: обзор не показываем
    block = ci.survey_block(_state(12000), ci.load_survey("s1"))
    assert "руины" in block and "(150,140)" in block and "MineableSteel" in block and "analyze_area" in block


def test_unsurveyed_new_colony_demands_survey():
    assert "survey_map" in ci.survey_block(_state(12000), None)
    assert ci.survey_block(_state(60000 * 40, buildings=30), None) == ""
