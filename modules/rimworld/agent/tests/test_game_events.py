"""События игры -> события модуля для ядра (ТЗ 11, RW-9): набег, гибель, пожар, письмо, цель; без дублей и без sender."""
import asyncio
import json

import pytest

import controller


class SpyLink:
    def __init__(self):
        self.events = []

    async def event(self, kind, data=None, text=None, urgency=None):
        self.events.append({"kind": kind, "data": data, "text": text, "urgency": urgency})


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    c = controller.Controller({"memory_dir": str(tmp_path), "rimworld_api": "http://localhost:8765"})
    c.link = SpyLink()
    return c


async def send(ctrl, evt):
    await ctrl._send_game_event(evt)
    await asyncio.sleep(0)                      # события уходят отдельными задачами
    await asyncio.sleep(0)
    return ctrl.link.events


async def test_incident_becomes_raid_with_urgency_by_priority(ctrl):
    events = await send(ctrl, {"type": "incident", "priority": "critical",
                               "data": {"incident": "RaidEnemy", "label": "налёт", "points": 120.0}})
    assert events[0]["kind"] == "raid" and events[0]["urgency"] == 5 and "налёт" in events[0]["text"]
    assert events[0]["data"]["incident"] == "RaidEnemy"
    events = await send(ctrl, {"type": "incident", "priority": "high", "data": {"label": "стая зверей"}})
    assert events[1]["kind"] == "raid" and events[1]["urgency"] == 4


async def test_death_fire_and_letter(ctrl):
    events = await send(ctrl, {"type": "pawn_died", "priority": "critical", "data": {"name": "Борис", "cause": "Bullet"}})
    assert events[-1]["kind"] == "death" and "Борис" in events[-1]["text"] and events[-1]["data"]["cause"] == "Bullet"
    events = await send(ctrl, {"type": "fire_started", "priority": "critical", "data": {"x": 3, "z": 4}})
    assert events[-1]["kind"] == "fire" and events[-1]["data"] == {"x": 3, "z": 4}
    events = await send(ctrl, {"type": "letter", "priority": "medium",
                               "data": {"label": "Торговцы", "letter_def": "TraderArrival", "text": "<b>Караван</b> пришёл"}})
    assert events[-1]["kind"] == "letter" and events[-1]["urgency"] == 2 and "Торговцы" in events[-1]["text"]
    assert "<b>" not in events[-1]["text"]                                            # теги очищены
    events = await send(ctrl, {"type": "letter", "priority": "high", "data": {"label": "Ссора", "letter_def": "NegativeEvent"}})
    assert events[-1]["urgency"] == 3


async def test_letters_about_raids_and_deaths_are_not_sent_twice(ctrl):
    for letter_def in ("ThreatBig", "ThreatSmall", "Death"):
        await send(ctrl, {"type": "letter", "priority": "critical", "data": {"label": "x", "letter_def": letter_def}})
    assert ctrl.link.events == []                                                       # о них уже сказали incident и pawn_died


async def test_other_game_events_are_ignored(ctrl):
    await send(ctrl, {"type": "pawn_downed", "priority": "high", "data": {"name": "Борис"}})
    ctrl.link = None
    await ctrl._send_game_event({"type": "incident", "data": {}})                      # без связи с ядром тихо ничего не делает


async def test_goal_closed_becomes_event_only_after_first_baseline(ctrl, tmp_path):
    goals = tmp_path / "goals.json"
    goals.write_text(json.dumps([{"id": "g1", "title": "Построить базу", "status": "active", "category": "ambition"},
                                 {"id": "g2", "title": "Запас еды", "status": "active", "category": "tactical"}]), encoding="utf-8")
    await ctrl._check_goals()
    assert ctrl.link.events == []                                                      # первая проверка только запоминает
    goals.write_text(json.dumps([{"id": "g1", "title": "Построить базу", "status": "completed", "category": "ambition",
                                  "progress_pct": 100},
                                 {"id": "g2", "title": "Запас еды", "status": "abandoned", "category": "tactical"},
                                 {"id": "g3", "title": "Новая", "status": "active"}]), encoding="utf-8")
    import os
    os.utime(goals, (goals.stat().st_atime, goals.stat().st_mtime + 5))                # файл изменился
    await ctrl._check_goals()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    by_id = {e["data"]["goal_id"]: e for e in ctrl.link.events}
    assert set(by_id) == {"g1", "g2"}
    assert by_id["g1"]["kind"] == "goal" and "достигнута" in by_id["g1"]["text"] and by_id["g1"]["data"]["status"] == "completed"
    assert "заброшена" in by_id["g2"]["text"]
    await ctrl._check_goals()                                                          # без изменений файла повторов нет
    assert len(ctrl.link.events) == 2
