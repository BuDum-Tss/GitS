"""Цикл целей: наблюдатель goal_watch, признак «цели нет» из снимка задач ядра (core_tasks), событие rimworld.goal_review и его проводка
в controller, вводная и манифест (без игры и сети)."""
import asyncio
import importlib
import re
import sys
from pathlib import Path

import pytest

import agent_activity
import controller
import core_link
import core_tasks
import events_log
import goal_watch
import module_manifest
import statediff
from goal_watch import ACTIVE_S, GRACE_S, GoalWatch, review_text

GOAL = {"id": "t1", "title": "Построить спальню на 4 кровати", "done": 1, "total": 3}
IMPERATIVES = re.compile(r"\b(сделай|построй|поставь|создай|выбери|назначь|добавь)\w*", re.I)


def run(w, now, **kw):
    kw = {"known": True, "goal": None, **kw}
    return w.update(now, **kw)


# ── goal_watch: когда шлёт ────────────────────────────────────────────────────

def test_no_goal_waits_grace_then_sends_once_per_min_interval():
    w = GoalWatch(min_s=240)
    assert run(w, 0) is None                                   # цели нет, но агент мог не успеть поставить новую
    assert run(w, GRACE_S - 1) is None
    first = run(w, GRACE_S)
    assert first["reason"] == "none" and first["goal"] == "" and first["closed"] == ""
    assert run(w, GRACE_S + 100) is None                       # чаще раза в goal_review_min_s нельзя
    assert run(w, GRACE_S + 239) is None
    assert run(w, GRACE_S + 240)["reason"] == "none"            # цели всё нет: повтор по интервалу


def test_closed_goal_is_reported_by_name():
    w = GoalWatch()
    run(w, 0, closed="Обустроить склад")
    res = run(w, GRACE_S, closed="Обустроить склад")
    assert res["reason"] == "closed" and res["closed"] == "Обустроить склад"
    assert "Цель «Обустроить склад» закрыта" in review_text(res, "кроватей 1 при 4 колонистах")


def test_new_goal_cancels_the_pending_review():
    w = GoalWatch()
    assert run(w, 0) is None
    assert run(w, 10, goal=GOAL, last_tool_at=9) is None       # агент поставил цель
    assert run(w, 30) is None                                  # цели снова нет: отсчёт заново
    assert run(w, 30 + GRACE_S)["reason"] == "none"


def test_silent_without_core_snapshot_in_danger_and_when_agent_paused_or_busy():
    w = GoalWatch()
    assert run(w, 0) is None
    assert run(w, 100, known=False) is None                     # ядро ещё не отдало снимок задач: о цели ничего не известно
    assert run(w, 101) is None                                 # после разрыва отсчёт заново
    assert run(w, 101 + GRACE_S, danger=True) is None          # бой и угроза
    assert run(w, 102 + GRACE_S) is None
    assert run(w, 300, agent_paused=True) is None              # пауза агента
    assert run(w, 301) is None
    w2 = GoalWatch()
    run(w2, 0)
    assert run(w2, 100, last_tool_at=100 - ACTIVE_S + 1) is None       # агент работает прямо сейчас
    assert run(w2, 100 + ACTIVE_S)["reason"] == "none"


def test_active_goal_without_actions_is_reported_as_stalled_only_after_idle_s():
    w = GoalWatch(idle_s=600)
    assert run(w, 0, goal=GOAL, last_tool_at=None) is None            # цель только что увидели
    assert run(w, 599, goal=GOAL, last_tool_at=None) is None
    res = run(w, 600, goal=GOAL, last_tool_at=None)
    assert res["reason"] == "stalled" and res["goal"] == GOAL["title"] and res["idle_s"] == 600 and (res["done"], res["total"]) == (1, 3)
    text = review_text(res, "поля нет, еды на 3 дня")
    assert "Активная цель «Построить спальню на 4 кровати» (шагов выполнено 1 из 3)" in text and "10 мин" in text
    w = GoalWatch(idle_s=600)
    run(w, 0, goal=GOAL)
    assert run(w, 700, goal=GOAL, last_tool_at=650) is None           # агент вызывал инструменты: цель идёт
    assert run(w, 1250, goal=GOAL, last_tool_at=650)["reason"] == "stalled"


def test_settings_override_intervals():
    w = GoalWatch(min_s=240)
    run(w, 0)
    assert run(w, GRACE_S, min_s=60) is not None
    assert run(w, GRACE_S + 30, min_s=60) is None
    assert run(w, GRACE_S + 60, min_s=60) is not None


def test_event_text_is_facts_without_orders():
    cases = [{"reason": "none", "goal": "", "closed": "", "idle_s": 0, "done": 0, "total": 0},
             {"reason": "closed", "goal": "", "closed": "Обустроить склад", "idle_s": 0, "done": 0, "total": 0},
             {"reason": "stalled", "goal": "Поле", "closed": "", "idle_s": 900, "done": 0, "total": 2}]
    gaps = "кроватей 1 при 4 колонистах; зоны склада нет: ресурсы без неё в учёте не видны; поля нет, еды на 3 дня"
    for res in cases:
        text = review_text(res, gaps)
        assert not IMPERATIVES.search(text), text
        assert "Состояние:" in text and gaps in text and text.endswith("Оценка колонии доступна инструментом colony_assess.")
    assert review_text(cases[0], gaps).startswith("Активной цели нет.")


# ── core_tasks: признак «цели нет» и «закрыта» из снимка ядра ──────────────────

def snapshot(*tasks, active=None):
    return {"workspace": "rimworld", "tasks": list(tasks), **({"active": active} if active else {})}


def task(tid, goal, progress=(0, 2)):
    return {"id": tid, "goal": goal, "status": "active", "progress": list(progress), "steps": []}


def teardown_function():
    core_tasks.set_snapshot(None)
    agent_activity.reset()


def test_core_tasks_known_closed_and_progress():
    assert core_tasks.known() is False and core_tasks.closed_goal() == ""
    core_tasks.set_snapshot(snapshot())                                  # ядро ответило: открытых задач нет
    assert core_tasks.known() is True and core_tasks.ambition() is None and core_tasks.closed_goal() == ""
    t = task("t1", "Построить спальню", (1, 3))
    core_tasks.set_snapshot(snapshot(t, active=t))
    assert core_tasks.goal_title() == "Построить спальню" and core_tasks.active_progress() == (1, 3)
    core_tasks.set_snapshot(snapshot())                                  # цель исчезла из открытых: закрыта
    assert core_tasks.closed_goal() == "Построить спальню" and core_tasks.active_progress() == (0, 0)
    core_tasks.set_snapshot(snapshot())
    assert core_tasks.closed_goal() == "Построить спальню"               # помним, пока нет новой
    t2 = task("t2", "Поле")
    core_tasks.set_snapshot(snapshot(t2, active=t2))
    assert core_tasks.closed_goal() == ""                                # новая цель: закрытая забыта


def test_goal_still_open_but_not_active_is_not_closed():
    t1, t2 = task("t1", "Склад"), task("t2", "Поле")
    core_tasks.set_snapshot(snapshot(t1, t2, active=t1))
    core_tasks.set_snapshot(snapshot(t1, t2, active=t2))                 # агент переключился: t1 ещё открыта
    assert core_tasks.closed_goal() == ""
    core_tasks.set_snapshot(snapshot(t1))                                # t2 закрыта, t1 осталась (но активной не названа)
    assert core_tasks.closed_goal() == "Поле"
    core_tasks.set_snapshot(None)
    assert core_tasks.known() is False and core_tasks.closed_goal() == ""


# ── controller: проводка события ────────────────────────────────────────────

class FakeLink:
    def __init__(self):
        self.sent = []
        self.module_settings = {}

    async def event(self, kind, data=None, text=None, urgency=None):
        self.sent.append((kind, data, text))


class Pol:
    def __init__(self, focus):
        self.in_focus = focus


def make(focus=True, link=True):
    c = controller.Controller.__new__(controller.Controller)
    c.link = FakeLink() if link else None
    c.policies = Pol(focus)
    return c


def lean_state(threats=(), mood=0.7):
    return {"tick": 1, "paused": False, "save_id": "s", "threats": list(threats),
            "colony": {"pawns": [{"id": "a", "name": "Кa", "current_job": "Research", "mood_pct": mood, "equipped": "Gun",
                                  "work_priorities": {"Construction": 3, "Hauling": 3, "Growing": 3}}],
                       "buildings": [], "zones": [], "resources": {}, "food_days": 2.0}}


async def drive(c, monkeypatch, times, state):
    it = iter(times)
    monkeypatch.setattr(controller.time, "time", lambda: next(it))
    monkeypatch.setattr(events_log, "log", lambda *a, **k: None)
    snap = statediff.snapshot(state)
    for _ in times:
        c._watch_goals(snap, state)
    await asyncio.sleep(0)


async def test_controller_sends_goal_review_with_facts_after_a_goal_is_closed(monkeypatch):
    core_tasks.set_snapshot(snapshot(task("t1", "Склад"), active=task("t1", "Склад")))
    core_tasks.set_snapshot(snapshot())
    c = make()
    await drive(c, monkeypatch, [0, GRACE_S + 1], lean_state())
    (kind, data, text), = c.link.sent
    assert kind == "goal_review" and data["reason"] == "closed" and data["closed"] == "Склад" and data["gaps"]
    assert text.startswith("Цель «Склад» закрыта, активной цели нет. Состояние: ")
    assert "colony_assess" in text and "еды на 2 дня" in text and not IMPERATIVES.search(text), text


async def test_controller_silent_in_battle_unfocused_without_link_or_snapshot(monkeypatch):
    core_tasks.set_snapshot(snapshot())
    c = make()
    await drive(c, monkeypatch, [0, GRACE_S + 1], lean_state(threats=[{"id": "t", "name": "Рейдер"}]))
    assert c.link.sent == []                                             # бой
    c = make(focus=False)
    await drive(c, monkeypatch, [0, GRACE_S + 1], lean_state())
    assert c.link.sent == []                                             # не в фокусе
    c = make(link=False)
    await drive(c, monkeypatch, [0, GRACE_S + 1], lean_state())          # без связи не падает
    core_tasks.set_snapshot(None)
    c = make()
    await drive(c, monkeypatch, [0, GRACE_S + 1], lean_state())
    assert c.link.sent == []                                             # ядро ещё не отдало задачи


async def test_controller_silent_while_agent_paused_the_game(monkeypatch):
    core_tasks.set_snapshot(snapshot())
    c = make()
    state = lean_state()
    state["paused"] = True
    agent_activity.mark_pause(0)
    await drive(c, monkeypatch, [1, GRACE_S + 2], state)
    assert c.link.sent == []


async def test_controller_uses_module_settings_for_the_interval(monkeypatch):
    core_tasks.set_snapshot(snapshot())
    c = make()
    c.link.module_settings = {"goal_review_min_s": 60}
    await drive(c, monkeypatch, [0, GRACE_S, GRACE_S + 30, GRACE_S + 60], lean_state())
    assert len(c.link.sent) == 2


# ── событие, настройки, манифест ─────────────────────────────────────────────

def test_event_is_declared_with_priority_50_ttl_300_replace_and_coalesce():
    assert core_link.EVENTS["goal_review"] == ("rimworld.goal_review", 3)
    assert core_link.EXPIRES_S["rimworld.goal_review"] == 300 and "rimworld.goal_review" in core_link.COALESCE_KINDS
    spec = {k["kind"]: k for k in module_manifest.event_kinds()}["rimworld.goal_review"]
    assert spec["default_priority"] == 50 and spec["ttl_s"] == 300 and spec["coalesce"] and spec["replace"]
    assert "critical" not in spec and "passive" not in spec
    emit = next(e for e in module_manifest.EMITS if e["kind"] == "rimworld.goal_review")
    assert emit["default_urgency"] == 3 and emit["follow_up"] == ["colony_assess"]
    assert not IMPERATIVES.search(emit["description"])
    root = str(Path(__file__).resolve().parents[4])
    sys.path.insert(0, root)
    try:
        kinds = importlib.import_module("agent.services.attention_v2.kinds")
    except Exception:
        pytest.skip("модуль внимания ядра недоступен")
    finally:
        sys.path.remove(root)
    ks = kinds.KindSpec.from_dict(spec)
    assert ks.replace and ks.coalesce and ks.ttl_s == 300


def test_settings_and_workspace_declare_self_goals():
    s = module_manifest.SETTINGS_SCHEMA
    assert s["goal_review_min_s"]["default"] == goal_watch.MIN_S == 240 and s["goal_review_idle_s"]["default"] == goal_watch.IDLE_S
    for key in ("goal_review_min_s", "goal_review_idle_s"):
        assert s[key]["type"] == "number" and s[key]["min"] < s[key]["default"] < s[key]["max"]
    ws = module_manifest.WORKSPACE
    assert ws["self_goals"] is True and ws["autonomous"] is True and ws["default_goal"].startswith("Развивать колонию")


async def test_manifest_with_self_goals_and_colony_assess_tool_is_valid():
    manifest = await module_manifest.build()
    assert manifest["workspace"]["self_goals"] is True
    tools = {t["name"]: t for t in manifest["tools"]}
    assert tools["colony_assess"]["read_only"] is True and 0 < len(tools["colony_assess"]["description"]) <= 300
    assert "node" not in tools["colony_assess"]                          # запрос глобален, как соседние обзоры colony_*
    assert {"colony_overview", "colony_assess"} <= set(tools)
    root = str(Path(__file__).resolve().parents[4])
    sys.path.insert(0, root)
    try:
        protocol = importlib.import_module("agent.interfaces.workspace.gits_sdk.protocol")
    except Exception:
        pytest.skip("контракт ядра недоступен")
    finally:
        sys.path.remove(root)
    assert protocol.validate_manifest(manifest) == []                    # self_goals и follow_up colony_assess контракт принимает
    bad = {**manifest, "workspace": {**manifest["workspace"], "self_goals": "да"}}
    assert any("self_goals" in p for p in protocol.validate_manifest(bad))
