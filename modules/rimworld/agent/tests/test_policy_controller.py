"""ТЗ 11e, POL-8 и POL-11/POL-12/POL-14 на контроллере: старт вне фокуса без запуска игры, цикл боя (пауза -> ход ->
срез -> пауза), ручное принуждение с порогом, лимитом и откатом, ступени скорости наблюдения, план с паузой и самопроверкой,
правило наблюдения, checkpoint при потере фокуса.

Игра, Claude и локальная модель не нужны: команды мода, состояние и ходы подменены. Пути к файлам памяти перенаправлены в tmp."""
import asyncio
import json
import time

import pytest

import controller
import focus_checkpoint
import game_modes
import planning_pause
import policy_events as pe
import policy_manager
import policy_actions as pa
import watch_rules
import local_agent
import statediff

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


async def test_module_starts_afk_and_never_launches_the_game(ctrl, monkeypatch):
    import game_launcher

    def no_launch(*a, **k):
        raise AssertionError("игра не должна запускаться при старте вне фокуса")
    monkeypatch.setattr(game_launcher, "launch", no_launch)
    monkeypatch.setattr(ctrl, "_tick_loop", lambda: asyncio.sleep(0))
    await ctrl._run_async()
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()
    assert ctrl.policies.current == "afk" and ctrl.sent == []


async def test_afk_tick_only_watches_and_sends_critical_events_with_needs_focus(ctrl, monkeypatch):
    sent_events = []

    class FakeLink:
        async def event(self, kind, data=None, text=None, urgency=None):
            sent_events.append((kind, data, urgency))
    ctrl.link = FakeLink()
    # режим core: фокус только от ядра, модуль сам его не берёт (правка fix/focus-bootstrap)
    monkeypatch.setattr(ctrl, "_module_setting", lambda key, default: "core" if key == "focus_mode" else default)
    monkeypatch.setattr(controller, "_get_state", lambda: state(threats=[threat()]))
    monkeypatch.setattr(controller, "_get_events", lambda: [
        {"type": "incident", "data": {"label": "пираты"}, "priority": "critical"},
        {"type": "letter", "data": {"label": "гости"}},
    ])
    stub = stub_turns(ctrl, monkeypatch)
    await ctrl._tick()
    await asyncio.sleep(0)
    assert ctrl.policies.current == "afk" and stub == [] and ctrl.sent == []      # ни ходов, ни команд игре
    assert [k for k, _, _ in sent_events] == ["game_started", "raid"]                              # критичное уходит ядру...
    assert sent_events[1][1]["needs_focus"] is True                                # ...с признаком «нужен фокус»


async def test_combat_cycle_pauses_decides_resumes_and_waits_for_the_slice(ctrl, monkeypatch):
    stub = stub_turns(ctrl, monkeypatch)
    monkeypatch.setattr(controller, "_get_state", lambda: st[0])
    monkeypatch.setattr(controller, "_get_events", lambda: [])
    ctrl._ensure_run_started = lambda s: None
    ctrl.policies.leave_afk("observe", "фокус")
    st = [state(tick=1000, threats=[threat()])]
    await ctrl._tick()                       # угроза: бой, первый ход с паузой
    await settle(ctrl)
    assert ctrl.policies.current == "combat"
    assert ctrl.sent == [{"type": "pause"}, {"type": "resume"}]                   # пауза -> ход -> снятие паузы
    assert ctrl._fight.turns == 1 and stub == ["combat"]
    await ctrl._tick()                       # тот же тик: срез ещё не прошёл, хода нет
    await settle(ctrl)
    assert ctrl._fight.turns == 1
    st[0] = state(tick=1000 + 700, threats=[threat()])                              # срез игры прошёл
    await ctrl._tick()
    await settle(ctrl)
    assert ctrl._fight.turns == 2 and ctrl.sent.count({"type": "pause"}) == 2       # второй круг той же схемой


async def test_combat_ends_only_after_the_threat_is_gone_for_quiet_seconds(ctrl, monkeypatch):
    stub_turns(ctrl, monkeypatch)
    monkeypatch.setattr(controller, "_get_events", lambda: [])
    ctrl.policies.leave_afk("observe", "")
    ctrl.policies.on_event("raid", 5, "набег")
    monkeypatch.setattr(controller, "_get_state", lambda: state(tick=500, threats=[]))
    await ctrl._tick()
    assert ctrl.policies.current == "combat"                                        # только что пропала: ещё бой
    ctrl._threat_gone_since = time.time() - 31
    await ctrl._tick()
    await settle(ctrl)
    assert ctrl.policies.current == "observe"


async def test_manual_policy_forces_a_stalled_task_once_then_rolls_back(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    (ctrl.tmp / "tasks.json").write_text(json.dumps(
        [{"description": "дерево у реки", "status": "pending", "x": 5, "z": 6, "work": "PlantCutting"}]), encoding="utf-8")
    snap = statediff.snapshot(state(tick=0, pawns=[pawn("p1")]))
    await ctrl._manual_watch(snap, 0)
    assert ctrl.policies.current == "observe" and ctrl.sent == []                  # ещё не застряла
    snap = statediff.snapshot(state(tick=2 * TICK, pawns=[pawn("p1")]))
    await ctrl._manual_watch(snap, 2 * TICK)                                        # застряла дольше 2 часов
    assert ctrl.policies.current == "manual"
    forces = [c for c in ctrl.sent if c["type"] == "force_work"]
    assert forces == [{"type": "force_work", "pawn_id": "p1", "targets": [{"x": 5, "z": 6}], "queue": False,
                       "work": "PlantCutting"}]
    await ctrl._manual_watch(snap, 2 * TICK + 1500)                                 # меньше интервала и отката: ничего
    assert len([c for c in ctrl.sent if c["type"] == "force_work"]) == 1
    assert not any(c["type"] == "clear_work_queue" for c in ctrl.sent)
    assert ctrl.policies.current == "manual"
    snap = statediff.snapshot(state(tick=2 * TICK + TICK + 100, pawns=[pawn("p1")]))
    await ctrl._manual_watch(snap, 2 * TICK + TICK + 100)                           # больше часа без движения: откат
    assert {"type": "clear_work_queue", "pawn_id": "p1", "stop_current": False} in ctrl.sent
    assert ctrl.policies.current == "observe"                                       # принуждение снято: выход


async def test_observe_speed_climbs_with_quiet_time_and_drops_on_important_event(ctrl):
    ctrl.policies.leave_afk("observe", "")
    now = time.time()
    ctrl._quiet_since, ctrl._last_speed_step = now - 500, now - 500
    await ctrl._apply_speed(False, now)
    assert ctrl.sent == [{"type": "set_time_speed", "speed": 2}]
    ctrl._last_speed_step = now - 500
    await ctrl._apply_speed(False, now)
    assert ctrl.sent[-1] == {"type": "set_time_speed", "speed": 3}                   # максимум по умолчанию 3
    ctrl._apply_policy_event(pe.make("raid", "набег", {"label": "x"}, importance=5))  # важное событие
    await ctrl._apply_speed(False, now)
    assert ctrl.sent[-1] == {"type": "set_time_speed", "speed": 1} and ctrl._speed == 1


async def test_planning_policy_pauses_plans_checks_and_returns_to_observe(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    monkeypatch.setattr(controller, "_get_state", lambda: state(tick=1000, paused=False))

    async def fake_plan(context):
        return {"actions": ["set_task(стена)"]}

    async def fake_check(context, result):
        return {"actions": ["set_task(стена, pending)"]}
    monkeypatch.setattr(controller, "run_planning", fake_plan)
    monkeypatch.setattr(controller, "run_plan_check", fake_check)
    ctrl._wake_planning("колонисты без дела")                                       # событие включает планирование
    assert ctrl.policies.current == "plan"
    result = await ctrl._plan_cycle()
    assert ctrl.sent == [{"type": "pause"}, {"type": "resume"}]
    assert result["actions"] == ["set_task(стена)", "set_task(стена, pending)"]     # план и самопроверка
    assert ctrl.policies.current == "observe" and not planning_pause.marked()


async def test_observe_turn_uses_the_local_model_and_no_claude(ctrl, monkeypatch):
    seen = {}

    async def fake_local(context, mode="observe", order=None, urgent=None, situation=None, max_steps=8, timeout=300, **kw):
        seen.update(context=context, max_steps=max_steps, timeout=timeout)
        return {"commentary": "", "actions": []}

    async def boom(*a, **k):
        raise AssertionError("Claude не должен вызываться в наблюдении")
    monkeypatch.setattr(local_agent, "run_local_turn", fake_local)
    monkeypatch.setattr(controller, "run_turn", boom)
    ctrl.policies.leave_afk("observe", "")
    ctrl._pending_events.append(pe.make("research_done", "исследование завершено: Орошение", {"research": "Орошение"}))
    await ctrl._mode_turn("observe", {}, [], None, False, None, None, None, ["Ада: здоровье 90% → 60%"])
    assert "=== ПОЛИТИКА: Наблюдение ===" in seen["context"] and "Орошение" in seen["context"]
    assert "полного снимка колонии здесь нет" in seen["context"]
    assert seen["max_steps"] == 8 and seen["timeout"] == 300


async def test_watch_rule_fires_on_research_and_switches_to_planning(ctrl):
    ctrl.policies.leave_afk("observe", "")
    rule, err = watch_rules.validate({"kind": "research_done", "field": "research", "value": "Орошение", "boost": 2,
                                      "switch_to": "plan", "ttl_h": 3, "why": "жду орошение для ферм"},
                                     registry=ctrl.policies.reg, tick=1000)
    assert err == "" and ctrl.watch.add(rule) == ""
    ctrl._tick_now = 1000
    ctrl._apply_policy_event(pe.make("research_done", "исследование завершено: Орошение", {"research": "Орошение"}))
    assert ctrl.policies.current == "plan"
    assert ctrl.watch.rules == []                                                   # сработало и снялось
    assert ctrl.policy_state()["watch_fired"][-1]["why"] == "жду орошение для ферм"
    json.dumps(ctrl.policy_state(), ensure_ascii=False)


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
