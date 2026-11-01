"""ТЗ 11e, POL-8 и POL-11/POL-12/POL-14 на контроллере: старт вне фокуса без запуска игры, цикл боя (пауза -> ход ->
срез -> пауза), ручное принуждение с порогом, лимитом и откатом, ступени скорости наблюдения, план с паузой и самопроверкой,
правило наблюдения, checkpoint при потере фокуса.

Игра, Claude и локальная модель не нужны: команды мода, состояние и ходы подменены. Пути к файлам памяти перенаправлены в tmp."""
import json

import pytest

import controller
import focus_checkpoint
import game_modes
import planning_pause
import policy_manager
import policy_actions as pa
import watch_rules

TICK = 2500


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(focus_checkpoint, "FILE", tmp_path / "checkpoint.json")
    monkeypatch.setattr(policy_manager, "REQUEST", tmp_path / "policy_request.json")
    monkeypatch.setattr(game_modes, "_REQUEST", tmp_path / "mode_request.json")
    monkeypatch.setattr(planning_pause, "_MARK", tmp_path / "planner_paused.json")
    monkeypatch.setattr(controller, "MEM_DIR", tmp_path)
    (tmp_path / "goals.json").write_text(json.dumps(
        [{"id": "g1", "category": "ambition", "status": "active", "title": "Выжить на берегу"}]), encoding="utf-8")
    sent: list[dict] = []
    monkeypatch.setattr(controller, "_game_command", lambda cmd: sent.append(cmd) or True)
    monkeypatch.setattr(controller, "_ping", lambda: False)
    c = controller.Controller({"memory_dir": str(tmp_path)})
    c._ensure_run_started = lambda state: None
    c.sent = sent
    c.tmp = tmp_path
    return c


def state(tick=1000, threats=(), paused=False, pawns=()):
    return {"tick": tick, "paused": paused, "save_id": "s1", "threats": [dict(t) for t in threats],
            "colony": {"pawns": [dict(p) for p in pawns], "resources": {}, "buildings": [], "pending_construction": []}}


def threat(tid="t1"):
    return {"id": tid, "name": "Пират", "kind": "man", "health_pct": 1.0, "position": {"x": 1, "z": 1}}


def pawn(pid="p1", job="Wait_Wander", **kw):
    return {"id": pid, "name": "Ада", "current_job": job, "mood_pct": 0.6, "health_pct": 1.0, **kw}


async def settle(c):
    if c._turn_task is not None:
        await c._turn_task


def stub_turns(c, monkeypatch):
    calls = []

    async def fake_mode_turn(*args, **kwargs):
        calls.append(args[0])
        return {"commentary": "", "actions": []}

    async def fake_finish(*args, **kwargs):
        return None
    monkeypatch.setattr(c, "_mode_turn", fake_mode_turn)
    monkeypatch.setattr(c, "_finish_turn", fake_finish)
    return calls






















async def test_focus_release_checkpoints_pauses_and_activate_restores(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    monkeypatch.setattr(controller, "_get_state", lambda: state(tick=1000, paused=False))
    rel = await ctrl.release({"reason": "фокус у монтажа"})
    assert rel["ok"] and rel["paused"] is True and rel["policy"] == "afk"
    assert ctrl.sent == [{"type": "pause"}] and ctrl.policies.current == "afk"
    cp = focus_checkpoint.load()
    assert cp["goal"] == "Выжить на берегу" and cp["paused_by_module"] is True and cp["policy"] == "observe"
    act = await ctrl.activate({"policy": "observe", "goal": "вернулась"})
    assert act == {"ok": True, "policy": "observe", "restored": True, "goal": "Выжить на берегу"}
    assert ctrl.sent[-1] == {"type": "resume"} and ctrl.policies.current == "observe"
    assert focus_checkpoint.load() is None                                          # восстановлено, checkpoint снят


async def test_release_twice_and_core_cannot_activate_combat(ctrl):
    assert (await ctrl.release({"reason": "x"}))["already"] is True                 # уже вне фокуса
    res = await ctrl.activate({"policy": "combat"})                                 # бой включают только события игры
    assert res["ok"] is False and ctrl.policies.current == "afk"


def test_launch_plan_is_pure_and_default_off():
    assert pa.launch_allowed(in_focus=False, autostart=True, seen_running_in_focus=True) is False
    assert pa.launch_allowed(in_focus=True, autostart=False, seen_running_in_focus=False) is False
