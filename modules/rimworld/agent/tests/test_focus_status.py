"""ТЗ 18 (FOC-1, FOC-4, MOD-32, MOD-33) на модуле RimWorld: блок `status` в `state` по политикам, прерываемость в бою и при
критическом ремонте, блокировка на исследовании, соответствие манифеста проверке SDK, `focus.resume` по checkpoint.

Игра, ядро и Claude не нужны: команды мода и состояние подменены, пути памяти перенаправлены в tmp."""
import json

import pytest
from glados_module import protocol as P

import controller
import focus_checkpoint
import focus_status
import game_modes
import module_manifest
import planning_pause
import policies
import policy_manager
import state_reporter
import watch_rules


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


def state(tick=1000, paused=False, buildings=()):
    return {"tick": tick, "paused": paused, "save_id": "s1", "threats": [],
            "colony": {"pawns": [], "resources": {}, "buildings": [dict(b) for b in buildings], "pending_construction": []}}


def write_tasks(tmp_path, tasks):
    (tmp_path / "tasks.json").write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")


# ── манифест и политики (FOC-1) ───────────────────────────────────────────────────────────────

def test_manifest_policies_pass_the_sdk_check_and_default_is_background():
    manifest = module_manifest.build_manifest([])
    assert P.validate_manifest(manifest) == []                     # проверка SDK ТЗ 18: фоновая по умолчанию и т.д.
    assert manifest["default_policy"] == "afk"
    by_id = {p["id"]: p for p in manifest["policies"]}
    assert by_id["afk"]["focus"] == "background" and by_id["combat"]["interruptible"] is False


def test_manifest_transitions_are_what_the_core_may_request():
    by_id = {p["id"]: p for p in policies.get().manifest()}
    assert by_id["afk"]["transitions"] == ["observe", "plan"]       # из фоновой только вернуть фокус (leave_afk)
    assert "combat" not in by_id["afk"]["transitions"]              # бой ядро не включает (POL-14)
    assert "afk" in by_id["combat"]["transitions"]                  # уход в фон и выход из боя
    assert "combat" in by_id["observe"]["transitions"]              # событие набега переводит из наблюдения в бой


def test_policy_urgency_and_resources_are_declared_per_policy():
    reg = policies.get()
    assert reg.policy("afk").urgency == 0.0 and reg.policy("afk").resources == {}
    assert reg.policy("combat").urgency == 1.0 and reg.policy("combat").resources == {"gpu": 1, "tokens": 1}
    assert reg.policy("observe").resources == {"gpu": 1}            # локальная модель - видеокарта
    assert reg.policy("plan").resources == {"tokens": 1}            # Claude - токены
    for p in reg.policies.values():
        assert 0 <= p.urgency <= 1


# ── статус по политикам (чистая функция) ──────────────────────────────────────────────────────

def test_status_in_each_policy_pure():
    reg = policies.get()
    afk = focus_status.build(reg.policy("afk"), goal="Цель", tasks=[], buildings=[])
    assert afk == {"active_policy": "afk", "active_goal": "Цель", "urgency": 0.0, "interruptible": True,
                   "estimated_remaining_s": None, "blocked": None, "resources": {}}
    observe = focus_status.build(reg.policy("observe"), goal="", tasks=[], buildings=[])
    assert observe["interruptible"] is True and observe["urgency"] == 0.1 and observe["resources"] == {"gpu": 1}
    plan = focus_status.build(reg.policy("plan"), goal="", tasks=[], buildings=[])
    assert plan["urgency"] == 0.4 and plan["resources"] == {"tokens": 1}


def test_status_is_json_serializable_for_the_state_message():
    reg = policies.get()
    for p in reg.policies.values():
        json.dumps(focus_status.build(p, goal="x", tasks=[{"status": "blocked", "description": "d"}], buildings=[]))


def test_interruptible_is_false_in_combat_even_without_damage():
    st = focus_status.build(policies.get().policy("combat"), goal="", tasks=[], buildings=[])
    assert st["interruptible"] is False and st["urgency"] == 1.0


def test_critical_repair_blocks_interrupting_observation_only_below_threshold():
    observe = policies.get().policy("observe")
    assert focus_status.interruptible(observe, []) is True                               # в наблюдении да
    assert focus_status.interruptible(observe, [{"label": "Стена", "hp_pct": 0.1}]) is False
    assert focus_status.interruptible(observe, [{"label": "Стена", "hp_pct": 0.5}]) is True
    assert focus_status.interruptible(observe, [{"label": "Стена"}]) is True               # нет данных о здоровье


def test_blocked_reason_names_the_research_or_the_blocked_step():
    plan = policies.get().policy("plan")
    waiting = [{"description": "ставить кухню", "status": "waiting_research", "waits_research": "Кухни"}]
    assert focus_status.build(plan, goal="", tasks=waiting, buildings=[])["blocked"] == "ждём исследование: Кухни"
    blocked = [{"description": "строить цепочку", "status": "blocked", "blocked_reason": "нет предпосылок"}]
    assert focus_status.build(plan, goal="", tasks=blocked, buildings=[])["blocked"] == (
        "шаг «строить цепочку»: нет предпосылок")
    afk = policies.get().policy("afk")                                                    # фон ничего не ждёт
    assert focus_status.build(afk, goal="", tasks=waiting, buildings=[])["blocked"] is None


# ── статус на контроллере и в сообщении state ─────────────────────────────────────────────────

async def test_controller_status_follows_the_policy(ctrl):
    assert ctrl.policy_state()["status"]["active_policy"] == "afk"
    ctrl.policies.leave_afk("observe", "")
    st = ctrl.policy_state()["status"]
    assert st["active_policy"] == "observe" and st["interruptible"] is True
    ctrl.policies.on_event("raid", 5, "набег")
    st = ctrl.policy_state()["status"]
    assert st["active_policy"] == "combat" and st["interruptible"] is False and st["urgency"] == 1.0


async def test_controller_status_sees_critical_repair_from_the_last_tick(ctrl):
    ctrl.policies.leave_afk("observe", "")
    ctrl._tick_state = state(buildings=[{"label": "Кухня", "hp_pct": 0.1}])
    assert ctrl.policy_state()["status"]["interruptible"] is False
    ctrl._tick_state = state(buildings=[{"label": "Кухня", "hp_pct": 0.9}])
    assert ctrl.policy_state()["status"]["interruptible"] is True


async def test_controller_status_reports_research_wait(ctrl):
    write_tasks(ctrl.tmp, [{"description": "ставить кухню", "status": "waiting_research", "waits_research": "Кухни"}])
    ctrl.policies.leave_afk("plan", "")
    assert ctrl.policy_state()["status"]["blocked"] == "ждём исследование: Кухни"


async def test_state_message_carries_status_as_data_status(ctrl, monkeypatch):
    class FakePanel:
        async def set(self, blocks):
            return False

    class FakeClient:
        panel = FakePanel()

        def __init__(self):
            self.sent = []

        async def state(self, summary, data=None):
            self.sent.append((summary, data))
            return True

    async def fake_build_state():
        return "игра: нет", {"game_phase": "none"}

    monkeypatch.setattr(state_reporter, "build_state", fake_build_state)
    ctrl.policies.leave_afk("observe", "")
    client = FakeClient()
    reporter = state_reporter.StateReporter(client, policy=ctrl.policy_state)
    await reporter.send_once()
    _, data = client.sent[-1]
    assert data["status"]["active_policy"] == "observe"
    assert set(data["status"]) == {"active_policy", "active_goal", "urgency", "interruptible", "estimated_remaining_s",
                                   "blocked", "resources"}


# ── focus.release возвращает checkpoint, focus.resume восстанавливает (FOC-4, MOD-32) ───────────

async def test_release_returns_the_checkpoint_to_the_core(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    monkeypatch.setattr(controller, "_get_state", lambda: state(paused=False))
    rel = await ctrl.release({"reason": "фокус у монтажа", "checkpoint": True})
    assert rel["ok"] is True and rel["checkpoint"]["policy"] == "observe"
    assert rel["checkpoint"]["goal"] == "Выжить на берегу"


async def test_resume_restores_from_the_core_checkpoint(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    monkeypatch.setattr(controller, "_get_state", lambda: state(paused=False))
    rel = await ctrl.release({"reason": "фокус у монтажа"})
    focus_checkpoint.clear()                                  # ядро помнит checkpoint сам, файл модуля может пропасть
    res = await ctrl.resume({"task_id": "t1", "policy": "observe", "goal": "Выжить на берегу",
                             "checkpoint": rel["checkpoint"], "subgoals": ["построить стену", 7]})
    assert res["ok"] is True and res["policy"] == "observe" and res["restored"] is True
    assert ctrl.policies.current == "observe"
    assert ctrl.sent[-1] == {"type": "resume"}                # игру, которую поставил модуль, снова запускаем
    assert "подцель: построить стену" in ctrl._recent_changes and len(ctrl._recent_changes) == 1


async def test_resume_without_checkpoint_still_takes_focus(ctrl):
    res = await ctrl.resume({"task_id": "t2", "policy": "plan", "goal": ""})
    assert res == {"ok": True, "policy": "plan", "restored": False, "goal": ""}
    assert ctrl.policies.current == "plan"


async def test_resume_when_already_in_focus_and_refusals(ctrl, monkeypatch):
    ctrl.policies.leave_afk("observe", "")
    assert (await ctrl.resume({"policy": "observe"}))["already"] is True
    ctrl.policies.enter_afk("сброс")
    bad = await ctrl.resume({"policy": "combat"})            # бой ядро не включает
    assert bad["ok"] is False and ctrl.policies.current == "afk"
    monkeypatch.setattr(ctrl, "_focus_mode", lambda: "manual")
    refused = await ctrl.resume({"policy": "observe"})
    assert refused["ok"] is False and ctrl.policies.current == "afk"


def test_attach_registers_focus_resume_handler():
    seen: dict = {}

    class FakeClient:
        def tool(self, name):
            return lambda fn: fn

        def on_request(self, name):
            def deco(fn):
                seen[name] = fn
                return fn
            return deco

        def panel_action(self, name):
            return lambda fn: fn

    class FakeFocus:
        async def activate(self, body):
            return {"ok": True}

        async def release(self, body):
            return {"ok": True}

        async def resume(self, body):
            return {"ok": True, "policy": "observe"}

        async def panel_take_focus(self, args):
            return {"ok": True}

        async def panel_release_focus(self, args):
            return {"ok": True}

    module_manifest.attach(FakeClient(), [], order=None, graph=None, focus=FakeFocus())
    assert {"policy.activate", "focus.release", "focus.resume"} <= set(seen)
