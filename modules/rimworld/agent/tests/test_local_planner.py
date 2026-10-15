"""Стартовый план на локальной модели: разбор ответа, формат файлов Claude, запись только при пустом плане."""
import asyncio
import json

import local_planner as lp

GOOD = json.dumps({
    "ambition": {"title": "Обустроить защищённую базу на 6 колонистов", "success_criteria": "6 колонистов, замкнутая крепость, еды на 20 дней"},
    "goals": [{"title": "Накопить запас еды", "category": "survival", "success_criteria": "еды на 15 дней"},
              {"title": "Построить спальню", "category": "nonsense", "success_criteria": "3 кровати в замкнутой комнате"}],
    "tasks": [{"description": "Расставить приоритеты работ у всех колонистов", "goal": 0},
              {"description": "Разведать карту и выбрать место для спальни", "goal": 1}]}, ensure_ascii=False)


def test_parse_rejects_bad_shapes():
    assert lp.parse("не json") is None
    assert lp.parse(json.dumps({"ambition": {"title": "коротко"}, "goals": []})) is None
    plan = lp.parse("вот план: " + GOOD + " готово")
    assert plan and len(plan["goals"]) == 2 and len(plan["tasks"]) == 2


def test_build_files_match_claude_format():
    goals, tasks = lp.build_files(lp.parse(GOOD), now=1790000000)
    assert goals[0]["category"] == "ambition" and goals[0]["priority"] == "high" and goals[0]["status"] == "active"
    assert goals[2]["category"] == "survival"                      # неизвестная категория заменена
    assert {"id", "title", "category", "success_criteria", "priority", "status", "progress_pct", "notes", "created", "updated"} <= set(goals[0])
    assert tasks[0]["goal_id"] == goals[1]["id"] and tasks[1]["goal_id"] == goals[2]["id"] and tasks[0]["status"] == "pending"


def test_seed_writes_only_when_no_goals(tmp_path, monkeypatch):
    monkeypatch.setattr(lp, "_DIR", tmp_path)
    monkeypatch.setattr(lp, "_last_try", [0.0])
    state = {"tick": 7000, "colony": {"pawns": [{"name": "Рика"}], "resources": {"WoodLog": 80}, "food_days": 4}}

    async def ask(system, user):
        assert "Колонистов: 1" in user and "WoodLog 80" in user      # модель видит факты, а не готовые цели
        return GOOD
    assert asyncio.run(lp.seed(state, ask)) is True
    assert json.loads((tmp_path / "goals.json").read_text(encoding="utf-8"))[0]["category"] == "ambition"
    assert len(json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))) == 2
    monkeypatch.setattr(lp, "_last_try", [0.0])
    assert asyncio.run(lp.seed(state, ask)) is False               # цели уже есть: ничего не трогаем
