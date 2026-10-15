"""Состояние модуля для ядра (ТЗ 11, RW-8, RW-20): game_running и summary."""
import asyncio

import state_reporter
from rigs import Rig

import game_api
import gamenet


def snapshot(connected=True, name="Дом", ambition="Построить базу"):
    return {"connected": connected, "colony_name": name if connected else None, "tick": 100 if connected else None,
            "pawns": [{"name": "Борис"}] if connected else [], "resources": {}, "research": {}, "goals": [],
            "tasks": [], "mode": {"mode": "observe", "label": "наблюдает"}, "pawn_fates": {},
            "ambition": {"title": ambition} if ambition else None}


def test_phase_and_summary_texts():
    assert state_reporter.phase_of(False, snapshot(False)) == "none"
    assert state_reporter.phase_of(True, snapshot(False)) == "menu"
    assert state_reporter.phase_of(True, snapshot(True)) == "colony"
    assert state_reporter.summary_of("colony", snapshot(), "действует") == \
        "игра: колония «Дом», режим агента: действует, цель: Построить базу"
    assert state_reporter.summary_of("menu", snapshot(False, ambition=None), "наблюдает") == \
        "игра: меню, режим агента: наблюдает, цель: не поставлена"
    assert state_reporter.summary_of("none", snapshot(False), "наблюдает").startswith("игра: нет,")


async def test_state_without_game_has_game_running_false(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: False)

    async def offline():
        return snapshot(False)
    monkeypatch.setattr(game_api, "game_snapshot", offline)
    async with Rig(tmp_path) as rig:
        await state_reporter.StateReporter(rig.client).send_once()
        body = (await rig.core.wait_message("state")).body
        assert body["data"]["game_running"] is False and body["data"]["game_phase"] == "none"
        assert body["summary"].startswith("игра: нет,")


async def test_state_with_colony_has_game_running_true_and_game_data(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: True)

    async def loaded():
        return snapshot(True)
    monkeypatch.setattr(game_api, "game_snapshot", loaded)
    async with Rig(tmp_path) as rig:
        await state_reporter.StateReporter(rig.client).send_once()
        body = (await rig.core.wait_message("state")).body
        assert body["data"]["game_running"] is True and body["data"]["colony_name"] == "Дом"
        assert body["data"]["pawns"] == [{"name": "Борис"}]            # то, что отдаёт /api/game
        assert "колония «Дом»" in body["summary"] and len(body["summary"]) <= 300


async def test_reporter_sends_periodically_and_on_poke(tmp_path, monkeypatch):
    monkeypatch.setattr(gamenet, "port_open", lambda *a, **k: False)

    async def offline():
        return snapshot(False)
    monkeypatch.setattr(game_api, "game_snapshot", offline)
    async with Rig(tmp_path) as rig:
        reporter = state_reporter.StateReporter(rig.client, interval=30)         # без poke следующее через 30 с
        task = asyncio.create_task(reporter.run())
        try:
            await rig.core.wait_message("state")
            reporter.poke()
            await rig.core.wait_message("state", nth=2)
        finally:
            task.cancel()
