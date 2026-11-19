"""Автоснятие паузы не трогает сознательную паузу агента и работу агента; опасность (упавшие, кровь, здоровье) блокирует."""
import pytest

import agent_activity
import controller
import events_log
import planning_pause
import speed_plan as sp
from speed_plan import plan_speed


@pytest.fixture(autouse=True)
def _clean_activity():
    agent_activity.reset()
    yield
    agent_activity.reset()


def st(speed=1, paused=False, threats=(), pawns=()):
    return {"time_speed": speed, "paused": paused, "threats": list(threats), "colony": {"pawns": list(pawns), "name": "К"}}


def paused_state(pawns=()):
    return st(1, paused=True, pawns=pawns)


DANGER_PAWNS = [
    {"id": "p", "is_downed": True},
    {"id": "p", "bleeding": 0.3},
    {"id": "p", "health_pct": 0.49},
    {"id": "p", "mental_state": "PanicFlee"},
    {"id": "p", "in_mental_break": True},
]


# ── опасность ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pawn", DANGER_PAWNS)
def test_colonist_trouble_is_danger_and_slows_to_normal(pawn):
    state = st(4, pawns=[pawn])
    assert sp.is_danger(state)
    assert plan_speed(state, "max", 100, 99, danger_at=100) == 1
    assert plan_speed({**state, "time_speed": 1}, "max", 1000, 0) is None          # поднимать нельзя
    assert plan_speed({**state, "time_speed": 2}, "max", 1000, 0) == 1


def test_healthy_and_prisoner_are_not_danger():
    assert not sp.is_danger(st(1, pawns=[{"id": "p", "health_pct": 0.5, "bleeding": 0, "is_downed": False}]))   # порог строгий
    assert not sp.is_danger(st(1, pawns=[{"id": "p", "is_prisoner": True, "is_downed": True, "health_pct": 0.1, "bleeding": 1}]))
    assert not sp.is_danger(st(1, pawns=[{"id": "p"}]))                                                      # полей нет


@pytest.mark.parametrize("pawn", DANGER_PAWNS)
def test_danger_blocks_pause_release(pawn):
    s = paused_state([pawn])
    assert plan_speed(s, "max", 1000, 0, paused_since=100) is None
    assert sp.decide(s, "max", 1000, 0, paused_since=100) == (None, sp.REASON_DANGER)


def test_max_returns_after_calm_s_following_colonist_danger():
    healed = st(1, pawns=[{"id": "p"}])
    assert plan_speed(healed, "max", 129, 0, danger_at=100) is None
    assert plan_speed(healed, "max", 130, 0, danger_at=100) == 4
    assert plan_speed(st(1, pawns=[{"id": "p", "is_downed": True}]), "max", 130, 0, danger_at=100) is None


def test_threat_blocks_pause_release_and_raise():
    assert plan_speed(st(1, paused=True, threats=[{"id": "t"}]), "max", 1000, 0, paused_since=100) is None
    assert plan_speed(st(1, threats=[{"id": "t"}]), "max", 1000, 0) is None


# ── метка паузы агента и активность ──────────────────────────────────────────

def test_fresh_agent_pause_is_not_released_old_one_is():
    s = paused_state()
    assert plan_speed(s, "max", 1000, 0, paused_since=100, agent_pause_at=500) is None
    assert sp.decide(s, "max", 1000, 0, paused_since=100, agent_pause_at=500) == (None, sp.REASON_AGENT_PAUSE)
    assert plan_speed(s, "max", 1099, 0, paused_since=100, agent_pause_at=500) is None            # 599 с: ещё свежая
    assert plan_speed(s, "max", 1100, 0, paused_since=100, agent_pause_at=500) == 4               # 600 с: забыта
    assert plan_speed(s, "max", 560, 0, paused_since=100, agent_pause_at=500, agent_hold_s=60) == 4
    assert plan_speed(s, "max", 559, 0, paused_since=100, agent_pause_at=500, agent_hold_s=60) is None


def test_recent_tool_call_blocks_release_and_raise():
    s = paused_state()
    assert plan_speed(s, "max", 1000, 0, paused_since=100, tool_at=950) is None
    assert sp.decide(s, "max", 1000, 0, paused_since=100, tool_at=950) == (None, sp.REASON_AGENT_ACTIVE)
    assert plan_speed(s, "max", 1040, 0, paused_since=100, tool_at=950) == 4                      # 90 с: уже не активен
    assert plan_speed(s, "max", 1000, 0, paused_since=100, tool_at=950, agent_active_s=0) == 4    # 0: не учитывать
    assert plan_speed(st(1), "max", 1000, 0, tool_at=950) is None                                 # скорость не поднимаем
    assert plan_speed(st(1), "max", 1040, 0, tool_at=950) == 4
    assert plan_speed(st(4, threats=[{"id": "t"}]), "max", 1000, 0, tool_at=999) == 1              # а снижать при опасности можно


def test_agent_activity_marks():
    agent_activity.note_command({"type": "pause"}, now=100)
    assert agent_activity.pause_is_fresh(699, 600) and not agent_activity.pause_is_fresh(700, 600)
    agent_activity.note_command({"type": "resume"})
    assert agent_activity.pause_at() is None
    agent_activity.note_command({"type": "set_time_speed", "speed": 0}, now=100)
    assert agent_activity.pause_at() == 100
    agent_activity.note_command({"type": "set_time_speed", "speed": 3})
    assert agent_activity.pause_at() is None
    agent_activity.note_command({"type": "set_time_speed", "speed": "x"})                         # мусор метку не меняет
    agent_activity.note_command({"type": "select"})
    assert agent_activity.pause_at() is None
    agent_activity.touch_tool(now=100)
    assert agent_activity.is_active(189, 90) and not agent_activity.is_active(190, 90) and not agent_activity.is_active(101, 0)


def test_mcp_tools_set_and_clear_agent_pause(monkeypatch):
    import httpx
    import rimworld_mcp

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "ok"}
    monkeypatch.setattr(httpx, "post", lambda *a, **k: R())
    monkeypatch.setattr(rimworld_mcp, "_recorded", lambda c, r: r)
    rimworld_mcp.pause_game()
    assert agent_activity.pause_at() is not None
    rimworld_mcp.resume_game()
    assert agent_activity.pause_at() is None
    rimworld_mcp.set_time_speed(0)
    assert agent_activity.pause_at() is not None
    rimworld_mcp.set_time_speed(2)
    assert agent_activity.pause_at() is None
    rimworld_mcp.pause_game()
    monkeypatch.setattr(R, "json", lambda self: {"status": "error", "message": "x"})            # неудачная команда метку не ставит
    agent_activity.clear_pause()
    rimworld_mcp.pause_game()
    assert agent_activity.pause_at() is None


async def test_attach_wrapper_touches_tool_time():
    import module_manifest

    async def handler(args):
        return "ok"
    wrapped = module_manifest._touching(handler)
    assert agent_activity.tool_at() is None
    assert await wrapped({}) == "ok"
    assert agent_activity.tool_at() is not None


# ── controller ────────────────────────────────────────────────────────────────

class Pol:
    in_focus = True


class FakeLink:
    module_settings: dict = {}


def make(monkeypatch):
    c = controller.Controller.__new__(controller.Controller)
    c.link = FakeLink()
    c.link.module_settings = {}
    c.policies = Pol()
    c._speed, c._speed_set_at, c._danger_at, c._defer_logged_at, c._paused_since = 1, 0.0, None, 0.0, None
    sent, logs = [], []
    monkeypatch.setattr(controller, "_game_command", lambda cmd: sent.append(cmd) or True)
    monkeypatch.setattr(planning_pause, "active", lambda *a, **k: False)
    monkeypatch.setattr(events_log, "log", lambda *a, **k: logs.append(a))
    monkeypatch.delenv("GITS_RIMWORLD_AUTO_SPEED", raising=False)
    monkeypatch.delenv("GITS_RIMWORLD_AUTO_UNPAUSE_S", raising=False)
    clock = [1000.0]
    monkeypatch.setattr(controller.time, "time", lambda: clock[0])
    return c, sent, logs, clock


async def test_controller_keeps_agent_pause_then_releases_after_hold(monkeypatch):
    c, sent, logs, clock = make(monkeypatch)
    s = paused_state()
    await c._auto_speed(s)
    agent_activity.mark_pause(1000.0)
    clock[0] += 200
    await c._auto_speed(s)
    assert sent == [] and len(logs) == 1 and "автоснятие паузы отложено: пауза агента" in logs[0][1]
    for _ in range(5):                                       # спама нет: не чаще раза в 5 минут
        clock[0] += 50
        await c._auto_speed(s)
    assert sent == [] and len(logs) == 1
    clock[0] += 60                                           # 510 с от метки, прошло больше 300 с от записи
    await c._auto_speed(s)
    assert len(logs) == 2 and sent == []
    clock[0] += 100                                          # 610 с > 600: пауза забыта
    await c._auto_speed(s)
    assert sent == [{"type": "set_time_speed", "speed": 4}] and "пауза снята автоматически" in logs[-1][1]


async def test_controller_active_agent_and_danger_defer_release(monkeypatch):
    c, sent, logs, clock = make(monkeypatch)
    await c._auto_speed(paused_state())
    clock[0] += 100
    agent_activity.touch_tool(clock[0] - 10)
    await c._auto_speed(paused_state())
    assert sent == [] and "агент активен" in logs[-1][1]
    clock[0] += 301                                          # агент молчит давно, запись в протокол снова разрешена
    await c._auto_speed(paused_state([{"id": "p", "is_downed": True}]))
    assert sent == [] and "опасность" in logs[-1][1]
    await c._auto_speed(paused_state())                      # упавшего подняли, но опасность была только что: скорость 1
    assert sent == [{"type": "set_time_speed", "speed": 1}]
    clock[0] += 31                                           # calm_s прошло: максимум возвращается
    await c._auto_speed(st(1))
    assert sent[-1] == {"type": "set_time_speed", "speed": 4}


async def test_controller_settings_are_clamped_and_stale_mark_cleared(monkeypatch):
    c, sent, logs, clock = make(monkeypatch)
    c.link.module_settings = {"agent_pause_hold_s": 5, "agent_active_s": 0, "auto_unpause_s": 9999}    # зажимаются: 60 / 0 / 600
    s = paused_state()
    agent_activity.touch_tool(1000.0)                        # agent_active_s = 0: не учитывается
    agent_activity.mark_pause(1000.0)
    await c._auto_speed(s)
    clock[0] += 599
    await c._auto_speed(s)
    assert sent == []                                        # auto_unpause_s зажато до 600
    clock[0] += 2
    await c._auto_speed(s)
    assert sent == [{"type": "set_time_speed", "speed": 4}]  # метка старше 60 с: забыта
    agent_activity.mark_pause(clock[0] - 2)                  # игра идёт: свежая метка остаётся, старая сбрасывается
    await c._auto_speed(st(4))
    assert agent_activity.pause_at() is not None
    clock[0] += 10
    await c._auto_speed(st(4))
    assert agent_activity.pause_at() is None
    c.link.module_settings = {"agent_pause_hold_s": "мусор"}
    assert c._num_setting("agent_pause_hold_s", 600, 60, 3600) == 600.0
    c.link.module_settings = {"agent_pause_hold_s": 99999}
    assert c._num_setting("agent_pause_hold_s", 600, 60, 3600) == 3600.0


async def test_controller_active_agent_blocks_raise(monkeypatch):
    c, sent, logs, clock = make(monkeypatch)
    agent_activity.touch_tool(995.0)
    await c._auto_speed(st(1))
    assert sent == []
    clock[0] += 100
    await c._auto_speed(st(1))
    assert sent == [{"type": "set_time_speed", "speed": 4}]


async def test_controller_downed_colonist_slows_game(monkeypatch):
    c, sent, logs, clock = make(monkeypatch)
    await c._auto_speed(st(4, pawns=[{"id": "p", "is_downed": True}]))
    assert sent == [{"type": "set_time_speed", "speed": 1}]


async def test_manifest_has_new_settings_and_defaults():
    import module_manifest
    schema = (await module_manifest.build())["settings_schema"]
    pick = lambda k: (schema[k]["default"], schema[k]["min"], schema[k]["max"])   # noqa: E731
    assert pick("auto_unpause_s") == (90, 0, 600)
    assert pick("agent_pause_hold_s") == (600, 60, 3600)
    assert pick("agent_active_s") == (90, 0, 600)
    assert schema["auto_speed"]["default"] == "max"
