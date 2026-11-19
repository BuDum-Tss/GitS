"""Рефлексы обороны и настроения в контроллере: хуки на подделках (без игры, сети и ядра)."""
import asyncio
import json

import pytest

import agent_activity
import controller
import core_link
import events_log
import module_manifest


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    agent_activity.reset()
    for name in ("GITS_RIMWORLD_THREAT_RESPONDER", "GITS_RIMWORLD_MORALE_KEEPER", "GITS_RIMWORLD_THREAT_DIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(events_log, "log", lambda *a, **k: None)
    yield
    agent_activity.reset()


class Pol:
    in_focus = True


class FakeLink:
    def __init__(self, settings=None):
        self.module_settings = settings or {}
        self.events = []

    async def event(self, kind, data=None, text=None, urgency=None):
        self.events.append((kind, data, text, urgency))


def make(monkeypatch, settings=None, items=()):
    c = controller.Controller.__new__(controller.Controller)
    c.link, c.policies = FakeLink(settings), Pol()
    sent = []

    def fake_request(cmd):
        sent.append(cmd)
        if cmd["type"] == "get_items":
            return True, json.dumps({"count": len(items), "items": list(items)})
        return True, ""
    monkeypatch.setattr(controller, "_game_request", fake_request)
    return c, sent


def pawn(pid, x=50, **kw):
    base = {"id": pid, "name": f"К{pid}", "health_pct": 1.0, "mood_pct": 0.7, "equipped": "Gun_Rifle", "skills": {"Shooting": 4},
            "position": {"x": x, "z": 50}, "schedule": ["Sleep"] * 6 + ["Anything"] * 14 + ["Joy"] * 2 + ["Sleep"] * 2}
    base.update(kw)
    return base


def raid_state(pawns):
    return {"tick": 1, "hour": 10, "colony": {"pawns": pawns},
            "threats": [{"id": "r1", "name": "Рейдер", "def": "Human", "kind": "human", "position": {"x": 80, "z": 50},
                         "health_pct": 1.0, "job": "AttackMelee"}]}


RIFLE = {"id": "w1", "def": "Gun_Rifle", "label": "винтовка", "x": 51, "z": 50, "ranged": True, "quality": "Good", "hp_pct": 1.0}


async def test_raid_arms_drafts_attacks_and_tells_core(monkeypatch):
    c, sent = make(monkeypatch, items=[RIFLE])
    threat = await c._threat_response(raid_state([pawn("a", equipped=None), pawn("b")]))
    await asyncio.sleep(0)
    assert threat is True
    kinds = [m["type"] for m in sent]
    assert kinds[0] == "get_items" and {"equip", "draft", "attack"} <= set(kinds)
    assert all(m.get("reflex") is True for m in sent)
    equip = next(m for m in sent if m["type"] == "equip")
    assert equip == {"type": "equip", "pawn_id": "a", "item_id": "w1", "reflex": True}
    ((kind, data, text, _),) = c.link.events
    assert kind == "threat_response" and text.startswith("Колонисты мобилизованы: Кa, Кb") and "оружие выдано: Кa (винтовка)" in text
    assert data["enemies"] == 1


async def test_second_pass_does_not_repeat_orders_within_cooldown(monkeypatch):
    c, sent = make(monkeypatch, items=[RIFLE])
    state = raid_state([pawn("a", equipped=None), pawn("b")])
    await c._threat_response(state)
    first = len(sent)
    await c._threat_response(state)
    assert len(sent) == first                                      # кулдауны: ни оружия, ни призыва, ни атаки заново


async def test_switch_env_and_setting_turn_the_reflex_off(monkeypatch):
    state = raid_state([pawn("a")])
    c, sent = make(monkeypatch)
    monkeypatch.setenv("GITS_RIMWORLD_THREAT_RESPONDER", "off")
    assert await c._threat_response(state) is True                 # угроза видна (настроение не трогаем), но приказов нет
    assert sent == [] and c.link.events == []
    monkeypatch.delenv("GITS_RIMWORLD_THREAT_RESPONDER")
    c2, sent2 = make(monkeypatch, settings={"threat_responder": False})
    await c2._threat_response(state)
    assert sent2 == []
    monkeypatch.setenv("GITS_RIMWORLD_THREAT_RESPONDER", "on")      # переменная важнее настройки
    await c2._threat_response(state)
    assert any(m["type"] == "draft" for m in sent2)


async def test_agent_combat_label_keeps_reflex_from_commanding(monkeypatch):
    c, sent = make(monkeypatch)
    agent_activity.note_command({"type": "attack", "pawn_id": "a", "target_id": "r1"})
    await c._threat_response(raid_state([pawn("a"), pawn("b")]))
    assert sent == []                                              # агент командует сам: призыв и атаки не навязываем


async def test_threat_response_distance_setting(monkeypatch):
    c, sent = make(monkeypatch, settings={"threat_response_dist": 20})
    await c._threat_response(raid_state([pawn("a")]))              # враг в 30 клетках, а радиус 20
    assert sent == []


async def test_downed_colonist_is_rescued_and_tended_after_the_fight(monkeypatch):
    c, sent = make(monkeypatch)
    pawns = [pawn("d", is_downed=True, health_pct=0.3, bleeding=0.3), pawn("m", skills={"Medicine": 10}), pawn("r")]
    await c._threat_response({"tick": 1, "colony": {"pawns": pawns}, "threats": []})
    assert {m["type"] for m in sent} == {"rescue", "tend"}
    assert next(m for m in sent if m["type"] == "tend")["pawn_id"] == "m"


async def test_morale_pass_sets_schedule_and_warns_core(monkeypatch):
    c, sent = make(monkeypatch)
    state = {"tick": 100000, "hour": 10, "threats": [],
             "colony": {"pawns": [pawn("a", mood_pct=0.2, schedule=["Anything"] * 24,
                                       mood_thoughts=[{"label": "Голод", "effect": -9}])]}}
    await c._morale_keep(state)
    await asyncio.sleep(0)
    assert {m["type"] for m in sent} == {"set_schedule"} and all(m["reflex"] for m in sent)
    ((kind, data, text, urgency),) = c.link.events
    assert kind == "morale_warning" and urgency == 3 and "Голод (у 1)" in text and data["total"] == 1


async def test_reflexes_run_skips_morale_during_threat_and_respects_switch(monkeypatch):
    c, sent = make(monkeypatch)
    state = raid_state([pawn("a", mood_pct=0.1, schedule=["Anything"] * 24)])
    await c._reflexes_run(state)
    assert not any(m["type"] == "set_schedule" for m in sent)       # в бою настроение не трогаем
    c2, sent2 = make(monkeypatch)
    monkeypatch.setenv("GITS_RIMWORLD_MORALE_KEEPER", "off")
    await c2._reflexes_run({"tick": 1, "hour": 10, "threats": [], "colony": {"pawns": [pawn("a", schedule=["Anything"] * 24)]}})
    assert sent2 == []


async def test_reflexes_do_not_stack_and_failures_do_not_raise(monkeypatch):
    c, _ = make(monkeypatch)
    gate = asyncio.Event()

    async def slow(state):
        await gate.wait()
    monkeypatch.setattr(c, "_reflexes_run", slow)
    c._reflexes({})
    first = c._reflex_task
    c._reflexes({})
    assert c._reflex_task is first                                  # второй проход поверх первого не запускается
    gate.set()
    await first

    def boom(cmd):
        raise RuntimeError("мод упал")
    c2, _ = make(monkeypatch)
    monkeypatch.setattr(controller, "_game_request", boom)
    await c2._reflexes_run(raid_state([pawn("a", equipped=None)]))  # ошибка уходит в журнал, тик не ломается


def test_game_request_returns_result_or_mod_error(monkeypatch):
    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data
    monkeypatch.setattr(controller, "_game_port_open", lambda: True)
    monkeypatch.setattr(controller.httpx, "post", lambda *a, **k: R({"status": "ok", "result": '{"items": []}'}))
    assert controller._game_request({"type": "get_items"}) == (True, '{"items": []}')
    monkeypatch.setattr(controller.httpx, "post", lambda *a, **k: R({"status": "error", "message": "нет кровати"}))
    assert controller._game_request({"type": "rescue"}) == (False, "нет кровати")
    monkeypatch.setattr(controller, "_game_port_open", lambda: False)
    assert controller._game_request({"type": "x"})[0] is False


# ── метки агента и виды событий ───────────────────────────────────────────────

def test_agent_activity_combat_and_schedule_labels():
    assert agent_activity.last_combat_order_ts() is None and agent_activity.last_schedule_order_ts() is None
    agent_activity.note_command({"type": "draft", "pawn_ids": ["a"]}, now=100)
    agent_activity.note_command({"type": "set_schedule", "pawn_id": "a"}, now=200)
    assert agent_activity.last_combat_order_ts() == 100 and agent_activity.last_schedule_order_ts() == 200
    agent_activity.note_command({"type": "attack", "reflex": True}, now=300)       # приказ самого рефлекса метку агента не двигает
    agent_activity.note_command({"type": "build"}, now=400)
    assert agent_activity.last_combat_order_ts() == 100
    agent_activity.note_command({"type": "pause"}, now=500)                        # прежняя метка паузы работает
    assert agent_activity.pause_at() == 500


def test_new_event_kinds_are_declared_with_the_agreed_urgency_ttl_and_passivity():
    assert core_link.EVENTS["threat_response"] == ("rimworld.threat_response", 2)
    assert core_link.EVENTS["morale_warning"] == ("rimworld.morale_warning", 3)
    kinds = {k["kind"]: k for k in module_manifest.event_kinds()}
    tr, mw = kinds["rimworld.threat_response"], kinds["rimworld.morale_warning"]
    assert tr["passive"] is True and tr["ttl_s"] == 60 and tr["max_priority"] < 30
    assert "passive" not in mw and mw["ttl_s"] == 300 and mw["replace"] is True and mw["coalesce"] is True
    assert {"threat_responder", "threat_response_dist", "morale_keeper"} <= set(module_manifest.SETTINGS_SCHEMA)
    assert module_manifest.SETTINGS_SCHEMA["threat_responder"]["default"] is True
    assert module_manifest.SETTINGS_SCHEMA["morale_keeper"]["default"] is True
