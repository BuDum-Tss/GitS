"""Автоскорость: чистая plan_speed (граничные случаи) и применение в controller на подделке."""
import pytest

import controller
import events_log
import planning_pause
import speed_plan as sp
from speed_plan import plan_speed


def st(speed=1, paused=False, threats=(), pawns=()):
    return {"time_speed": speed, "paused": paused, "threats": list(threats), "colony": {"pawns": list(pawns)}}


def test_raises_to_max_when_calm():
    assert plan_speed(st(1), "max", 100, 0) == 4
    assert plan_speed(st(3), "max", 100, 0) == 4
    assert plan_speed(st(4), "max", 100, 0) is None


def test_off_pause_and_planning_pause_do_nothing():
    assert plan_speed(st(1), "off", 100, 0) is None
    assert plan_speed(st(1, paused=True), "max", 100, 0) is None
    assert plan_speed(st(1), "max", 100, 0, paused_by_planning=True) is None
    assert plan_speed({"paused": False, "threats": []}, "max", 100, 0) is None       # нет скорости (меню)


def test_cooldown_20s():
    assert plan_speed(st(1), "max", 119, 100) is None
    assert plan_speed(st(1), "max", 120, 100) == 4


@pytest.mark.parametrize("state", [st(4, threats=[{"id": "t"}]), st(4, pawns=[{"id": "p", "is_drafted": True}]),
                                   st(4, pawns=[{"id": "p", "current_job": "AttackMelee"}])])
def test_danger_slows_to_normal_without_cooldown(state):
    assert plan_speed(state, "max", 100, 99, danger_at=100) == 1
    assert plan_speed({**state, "time_speed": 1}, "max", 100, 99, danger_at=100) is None


def test_max_returns_after_30s_of_calm():
    calm = st(1)
    assert plan_speed(calm, "max", 129, 0, danger_at=100) is None
    assert plan_speed(calm, "max", 130, 0, danger_at=100) == 4


def test_normalize():
    assert sp.normalize("OFF") == "off" and sp.normalize("") == "max" and sp.normalize("мусор") == "max"


# ── controller ────────────────────────────────────────────────────────────────

class Pol:
    def __init__(self, focus=True):
        self.in_focus = focus


class FakeLink:
    module_settings: dict = {}


def make(monkeypatch, focus=True, mode=None):
    c = controller.Controller.__new__(controller.Controller)
    c.link = FakeLink()
    c.policies = Pol(focus)
    c._speed, c._speed_set_at, c._danger_at = 1, 0.0, None
    sent = []
    monkeypatch.setattr(controller, "_game_command", lambda cmd: sent.append(cmd) or True)
    monkeypatch.setattr(planning_pause, "active", lambda *a, **k: False)
    monkeypatch.setattr(events_log, "log", lambda *a, **k: None)
    monkeypatch.delenv("GITS_RIMWORLD_AUTO_SPEED", raising=False)
    if mode:
        monkeypatch.setenv("GITS_RIMWORLD_AUTO_SPEED", mode)
    return c, sent


async def test_controller_sets_max_then_slows_on_threat_and_returns(monkeypatch):
    c, sent = make(monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(controller.time, "time", lambda: clock[0])
    await c._auto_speed(st(1))
    assert sent == [{"type": "set_time_speed", "speed": 4}]
    clock[0] += 5
    await c._auto_speed(st(4, threats=[{"id": "t"}]))
    assert sent[-1] == {"type": "set_time_speed", "speed": 1}
    clock[0] += 25
    await c._auto_speed(st(1))
    assert len(sent) == 2                    # спокойно только 25 с
    clock[0] += 10
    await c._auto_speed(st(1))
    assert sent[-1] == {"type": "set_time_speed", "speed": 4}


async def test_controller_off_not_in_focus_and_env_override(monkeypatch):
    c, sent = make(monkeypatch, mode="off")
    await c._auto_speed(st(1))
    c2, sent2 = make(monkeypatch, focus=False)
    await c2._auto_speed(st(1))
    assert sent == [] and sent2 == []


# ── снятие забытой паузы ──────────────────────────────────────────────────────

def paused_state(**kw):
    return {**st(1, paused=True, **kw), "colony": {"pawns": [], "name": "К"}}


def test_forgotten_pause_is_released_only_after_threshold():
    s = paused_state()
    assert plan_speed(s, "max", 139, 0, paused_since=100) is None                 # 39 с: ещё не забытая
    assert plan_speed(s, "max", 140, 0, paused_since=100) == 4
    assert plan_speed(s, "max", 1000, 0, paused_since=None) is None
    assert plan_speed(s, "max", 1000, 0, paused_since=100, unpause_after_s=0) is None      # 0: выключено
    assert plan_speed(s, "max", 150, 0, paused_since=100, unpause_after_s=10) == 4
    assert plan_speed(s, "max", 140, 0, paused_since=100, danger_at=130) == 1              # недавняя угроза: обычная


def test_pause_release_is_blocked_by_planning_threat_menu_off_and_cooldown():
    s = paused_state()
    assert plan_speed(s, "max", 200, 0, paused_by_planning=True, paused_since=100) is None
    assert plan_speed(paused_state(threats=[{"id": "t"}]), "max", 200, 0, paused_since=100) is None
    assert plan_speed({"paused": True, "threats": []}, "max", 200, 0, paused_since=100) is None         # нет колонии (меню)
    assert plan_speed(s, "off", 200, 0, paused_since=100) is None
    assert plan_speed(s, "max", 119, 100, paused_since=0) is None                           # кулдаун 20 с
    assert plan_speed(s, "max", 120, 100, paused_since=0) == 4


async def test_controller_releases_forgotten_pause_and_logs(monkeypatch):
    c, sent = make(monkeypatch)
    c._paused_since = None
    logs = []
    monkeypatch.setattr(events_log, "log", lambda *a, **k: logs.append(a))
    clock = [1000.0]
    monkeypatch.setattr(controller.time, "time", lambda: clock[0])
    s = paused_state()
    await c._auto_speed(s)
    clock[0] += 39
    await c._auto_speed(s)
    assert sent == []
    clock[0] += 2
    await c._auto_speed(s)
    assert sent == [{"type": "set_time_speed", "speed": 4}] and "пауза снята автоматически: простояла 41 с" in logs[-1][1]


async def test_controller_pause_release_can_be_disabled_by_env(monkeypatch):
    c, sent = make(monkeypatch)
    c._paused_since = None
    monkeypatch.setenv("GITS_RIMWORLD_AUTO_UNPAUSE_S", "0")
    clock = [1000.0]
    monkeypatch.setattr(controller.time, "time", lambda: clock[0])
    await c._auto_speed(paused_state())
    clock[0] += 500
    await c._auto_speed(paused_state())
    assert sent == []
