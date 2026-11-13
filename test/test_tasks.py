"""Общие задачи (ТЗ 32, WS-8): домен `domain/tasks.py`, TaskStore, TaskService, Planner, напоминания и планы как задачи, перенос
`reminders.json`, инструменты `assistant.tasks|create_task|close_task`. Без сети и моделей: модель, часы и журнал заглушены."""
import json
from types import SimpleNamespace

import pytest

from adapters.task_migration import migrate_reminders
from adapters.task_reminders import TaskReminders
from adapters.task_store import TaskStore
from app.impulse_timer import ImpulseTimer
from app.plan_nudge import PlanNudger
from app.planner import LlmPlanner, parse_steps
from app.task_service import TaskError, TaskService
from contracts import Planner, ReminderRepository, TaskRepository
from domain import tasks as T
from environments import builtin
from environments.base import ToolError
from environments.registry import Registry

NOW = 100_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakePlanner:
    def __init__(self, steps=("найти материалы", "собрать", "проверить")):
        self.steps, self.calls = list(steps), []

    async def plan(self, goal, workspace_ctx):
        self.calls.append((goal, workspace_ctx))
        return [{"text": s} for s in self.steps]


def service(planner=None, ws="assistant", clock=None):
    log = []
    cur = {"ws": ws}
    svc = TaskService(TaskStore(":memory:"), planner=planner, workspace_of=lambda: cur["ws"], clock=clock or Clock(),
                      log=lambda kind, text="", **d: log.append((kind, d)))
    svc.log, svc.cur = log, cur
    return svc


# ── домен ─────────────────────────────────────────────────────────────────────

def task(i="t1", **kw):
    return T.new_task(i, kw.pop("workspace", "assistant"), kw.pop("goal", "цель"), NOW, **kw)


def test_new_task_cleans_steps_and_priority():
    t = T.new_task("a", "assistant", "  цель  ", NOW, steps=["один", "", "Один", {"text": "два"}], priority=99, origin="чужой")
    assert t.goal == "цель" and [s.text for s in t.steps] == ["один", "два"] and t.priority == 5 and t.origin == T.USER


def test_task_dict_roundtrip():
    t = task(steps=["а", "б"], due=NOW + 5, kind=T.KIND_REMINDER, meta={"x": 1}, origin=T.SELF)
    assert T.Task.from_dict(t.to_dict()) == t


def test_next_step_and_finish_closes_task():
    t = task(steps=["а", "б"])
    assert T.next_step(t).text == "а"
    t = T.finish_step(t, None, NOW + 1)
    assert T.next_step(t).text == "б" and t.status == T.PENDING and T.progress(t) == (1, 2)
    t = T.finish_step(t, None, NOW + 2)
    assert t.status == T.DONE and T.next_step(t) is None
    assert T.finish_step(t, None, NOW + 3) is t               # закрытую не трогаем


def test_terminal_status_is_final():
    t = T.with_status(task(), T.DONE, NOW + 1)
    assert T.with_status(t, T.ACTIVE, NOW + 2).status == T.DONE
    assert not T.can_transition(T.CANCELLED, T.PENDING) and T.can_transition(T.BLOCKED, T.ACTIVE)


@pytest.mark.parametrize("kids,expected", [
    ([], T.PENDING),
    ([T.DONE, T.DONE], T.DONE),
    ([T.DONE, T.CANCELLED], T.DONE),
    ([T.CANCELLED, T.CANCELLED], T.CANCELLED),
    ([T.DONE, T.FAILED], T.FAILED),
    ([T.DONE, T.ACTIVE], T.ACTIVE),
    ([T.PENDING, T.WAITING], T.WAITING),
    ([T.BLOCKED, T.BLOCKED], T.BLOCKED),
    ([T.BLOCKED, T.PENDING], T.PENDING),
    ([T.DONE, T.PENDING], T.PENDING),
])
def test_parent_status_from_children(kids, expected):
    assert T.parent_status(T.PENDING, kids) == expected


def test_parent_terminal_stays_and_descendants_survive_cycles():
    assert T.parent_status(T.DONE, [T.PENDING]) == T.DONE
    a, b = task("a", parent="b"), task("b", parent="a")
    assert {t.id for t in T.descendants([a, b], "a")} == {"b"}


def test_can_interrupt_only_in_safe_point_when_active():
    assert T.can_interrupt(None) and T.can_interrupt(task()) and not T.can_interrupt(T.with_status(task(), T.ACTIVE, NOW), False)
    assert T.can_interrupt(T.with_status(task(), T.ACTIVE, NOW), True)


def test_pick_active_prefers_active_then_priority_then_due_and_works_on_leaves():
    low, high = task("l", priority=2), task("h", priority=5)
    soon, later = task("s", priority=3, due=NOW + 1), task("z", priority=3, due=NOW + 99)
    assert T.pick_active([low, high], "assistant").id == "h"
    assert T.pick_active([later, soon], "assistant").id == "s"
    assert T.pick_active([low, T.with_status(high, T.WAITING, NOW), ], "assistant").id == "l"
    running = T.with_status(low, T.ACTIVE, NOW)
    assert T.pick_active([running, high], "assistant").id == "l"
    parent, kid = task("p", priority=5), task("k", parent="p", priority=1)
    assert T.pick_active([parent, kid], "assistant").id == "k"
    assert T.pick_active([high], "rimworld") is None
    assert T.pick_active([task("r", kind=T.KIND_REMINDER)], "assistant") is None


def test_due_for_nudge_window_and_order():
    soon = task("a", due=NOW + 600)
    far = task("b", due=NOW + 10_000)
    old = task("c", due=NOW - 8 * 24 * 3600)
    closed = T.with_status(task("d", due=NOW), T.DONE, NOW)
    nodue = task("e")
    got = T.due_for_nudge([far, soon, old, closed, nodue], NOW, lead_s=1800)
    assert [t.id for t in got] == ["a"]
    assert T.next_due([far, soon, closed]) == NOW + 600


def test_checkpoint_lists_open_tasks_without_reminders():
    snap = T.checkpoint_of([task("a", steps=["x"]), task("r", kind=T.KIND_REMINDER), T.with_status(task("d"), T.DONE, NOW)],
                           "assistant", "цель", NOW)
    assert [t["id"] for t in snap["tasks"]] == ["a"] and snap["goal"] == "цель"


# ── Planner ──────────────────────────────────────────────────────────────────

def test_parse_steps_json_and_lists():
    assert [s["text"] for s in parse_steps('вот: {"steps": ["один", "два", "один"]} ок')] == ["один", "два"]
    assert [s["text"] for s in parse_steps("1. найти\n2) собрать\n- проверить")] == ["найти", "собрать", "проверить"]
    assert parse_steps("") == []


async def test_llm_planner_uses_model_and_survives_failure():
    asked = {}

    class Llm:
        async def ask(self, system, user, **kw):
            asked.update(system=system, user=user, **kw)
            return '{"steps": ["а", "б"]}'

    p = LlmPlanner(Llm(), lambda: {"system": "S", "format": "F"})
    assert isinstance(p, Planner)
    assert [s["text"] for s in await p.plan("сделать", {"workspace": "assistant"})] == ["а", "б"]
    assert "сделать" in asked["user"] and asked["system"] == "S" and asked["background"] is True

    class Broken:
        async def ask(self, *a, **k):
            raise RuntimeError("нет модели")

    assert await LlmPlanner(Broken(), lambda: {"system": "S", "format": "F"}).plan("x", {}) == []


# ── TaskStore ────────────────────────────────────────────────────────────────

def test_store_roundtrip_filters_and_contract():
    s = TaskStore(":memory:")
    assert isinstance(s, TaskRepository)
    a, b = task("a", workspace="rimworld", steps=["x"]), T.with_status(task("b", parent="a"), T.DONE, NOW)
    s.save(a.to_dict())
    s.save(b.to_dict())
    assert T.Task.from_dict(s.get("a")) == a
    assert [t["id"] for t in s.list(workspace="rimworld")] == ["a"]
    assert [t["id"] for t in s.list(statuses=(T.DONE,))] == ["b"]
    assert [t["id"] for t in s.list(parent="a")] == ["b"]
    s.delete("a")
    assert s.get("a") is None


def test_store_survives_reopen_and_follows_path(tmp_path):
    folder = {"p": tmp_path / "one"}
    s = TaskStore(lambda: folder["p"] / "tasks.db")
    s.save(task("a").to_dict())
    folder["p"] = tmp_path / "two"                              # переключили персонажа
    assert s.get("a") is None
    s.save(task("b").to_dict())
    folder["p"] = tmp_path / "one"
    assert s.get("a") is not None and s.get("b") is None


def test_store_keeps_limited_history_of_closed(monkeypatch):
    import adapters.task_store as ts
    monkeypatch.setattr(ts, "KEEP_CLOSED", 3)
    s = TaskStore(":memory:")
    for i in range(6):
        d = T.with_status(task(f"c{i}"), T.DONE, NOW + i).to_dict()
        s.save(d)
    s.save(task("open").to_dict())
    assert len(s.list(statuses=(T.DONE,))) == 3 and s.get("open") is not None


# ── TaskService ──────────────────────────────────────────────────────────────

async def test_create_plans_with_planner_and_logs_event():
    svc = service(FakePlanner())
    t = await svc.create("собрать ролик", plan=True)
    assert [s.text for s in t.steps] == ["найти материалы", "собрать", "проверить"] and t.workspace == "assistant"
    assert svc.planner.calls[0][1]["workspace"] == "assistant"
    assert svc.log[0][0] == "task.created" and svc.log[0][1]["id"] == t.id


async def test_create_without_planner_or_goal():
    svc = service()
    assert (await svc.create("просто", plan=True)).steps == ()
    with pytest.raises(TaskError):
        await svc.create("  ")
    with pytest.raises(TaskError):
        await svc.create("x", parent="нет")


async def test_split_makes_children_and_parent_follows_them():
    svc = service(FakePlanner(["а", "б"]))
    parent = await svc.create("цель", workspace="montage", origin=T.SELF, priority=4)
    kids = await svc.split(parent.id)
    assert [k.goal for k in kids] == ["а", "б"] and all(k.parent == parent.id and k.workspace == "montage" for k in kids)
    await svc.set_status(kids[0].id, T.ACTIVE)
    assert (await svc.get(parent.id)).status == T.ACTIVE
    await svc.close(kids[0].id)
    await svc.close(kids[1].id)
    assert (await svc.get(parent.id)).status == T.DONE


async def test_only_one_active_task_per_workspace():
    svc = service()
    a, b = await svc.create("а"), await svc.create("б")
    await svc.activate(a.id)
    await svc.activate(b.id)
    assert (await svc.get(a.id)).status == T.PENDING and (await svc.get(b.id)).status == T.ACTIVE
    assert (await svc.active()).id == b.id


async def test_close_rules_and_cancel_cascades():
    svc = service()
    p = await svc.create("родитель")
    k = await svc.create("ребёнок", parent=p.id)
    await svc.close(p.id, status=T.CANCELLED)
    assert (await svc.get(k.id)).status == T.CANCELLED
    with pytest.raises(TaskError):
        await svc.close(p.id)                                   # уже закрыта
    with pytest.raises(TaskError):
        await svc.close(k.id, status=T.ACTIVE)
    with pytest.raises(TaskError):
        await svc.close("нет такой")


async def test_finish_step_to_done_and_events():
    svc = service()
    t = await svc.create("дело", steps=["а", "б"])
    t = await svc.finish_step(t.id)
    assert T.progress(t) == (1, 2)
    t = await svc.finish_step(t.id)
    assert t.status == T.DONE
    kinds = [k for k, _ in svc.log]
    assert "task.step" in kinds and "task.status" in kinds


async def test_list_is_per_workspace_and_hides_reminders_in_snapshot():
    svc = service()
    await svc.create("ассистентское")
    await svc.create("игровое", workspace="rimworld")
    svc.reminders = TaskReminders(svc.repo, workspace_of=lambda: svc.cur["ws"], clock=svc.clock)
    svc.reminders.add(NOW + 60, "позже")
    assert sorted(t.goal for t in await svc.list_tasks()) == ["ассистентское", "позже"]
    assert [t.goal for t in await svc.list_tasks(kinds=(T.KIND_TASK,))] == ["ассистентское"]
    svc.cur["ws"] = "rimworld"
    assert [t.goal for t in await svc.list_tasks()] == ["игровое"]
    assert len(await svc.list_tasks("*")) == 3
    snap = await svc.snapshot("assistant")
    assert [t["goal"] for t in snap["tasks"]] == ["ассистентское"] and snap["active"]["goal"] == "ассистентское"


async def test_checkpoint_on_leave_and_resume_on_enter():
    svc = service()
    t = await svc.create("играть по плану", workspace="rimworld", steps=["а", "б"], priority=4)
    await svc.activate(t.id)
    other = await svc.create("менее важное", workspace="rimworld", priority=2)
    snap = await svc.checkpoint("rimworld", "колония")
    assert snap["goal"] == "колония" and {x["id"] for x in snap["tasks"]} == {t.id, other.id}
    saved = await svc.get(t.id)
    assert saved.status == T.PENDING and saved.checkpoint["tasks"]
    resumed = await svc.resume("rimworld")
    assert resumed.id == t.id and resumed.status == T.ACTIVE and resumed.checkpoint is None
    assert await svc.resume("montage") is None
    assert [k for k, _ in svc.log if k in ("task.checkpoint", "task.resumed")] == ["task.checkpoint", "task.resumed"]


async def test_workspace_change_hook_checkpoints_and_resumes():
    import asyncio
    svc = service()
    t = await svc.create("игровая цель", workspace="rimworld")
    await svc.activate(t.id)
    svc.on_workspace_change({"from": "rimworld", "to": "assistant", "goal": ""})
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert (await svc.get(t.id)).status == T.PENDING
    svc.on_workspace_change({"from": "assistant", "to": "rimworld", "goal": "вернулась"})
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert (await svc.get(t.id)).status == T.ACTIVE


async def test_upsert_plan_mirrors_and_updates_due():
    svc = service()
    p = await svc.upsert_plan("m1", "сдать курс", NOW + 100)
    assert p.kind == T.KIND_PLAN and p.origin == T.USER and p.due == NOW + 100
    again = await svc.upsert_plan("m1", "сдать курс", NOW + 100)
    assert again.id == p.id and len(await svc.list_tasks("*")) == 1
    moved = await svc.upsert_plan("m1", "сдать курс", NOW + 500)
    assert moved.id == p.id and moved.due == NOW + 500
    assert [t.id for t in await svc.due_for_nudge(lead_s=1000)] == [p.id]


# ── напоминания себе это задачи ──────────────────────────────────────────────

def test_task_reminders_follow_the_reminder_contract():
    repo = TaskStore(":memory:")
    log = []
    r = TaskReminders(repo, clock=Clock(), log=lambda kind, text="", **d: log.append(kind))
    assert isinstance(r, ReminderRepository)
    rec = r.add(NOW + 60, "проверить почту", source={"kind": "conversation"}, depth=1, untrusted=True, ttl_s=5)
    assert rec["status"] == "pending" and rec["depth"] == 1 and rec["untrusted"] and rec["ttl_s"] == 5
    assert repo.get(rec["id"])["kind"] == T.KIND_REMINDER and repo.get(rec["id"])["origin"] == T.SELF
    assert r.pending()[0]["id"] == rec["id"] and r.get(rec["id"])["text"] == "проверить почту"
    r.set_status(rec["id"], "fired")
    assert r.pending() == [] and r.get(rec["id"])["status"] == "fired"
    r.set_status(rec["id"], "pending")                          # закрытое не оживает
    assert r.get(rec["id"])["status"] == "fired"
    plan = r.add(NOW, "о плане", source={"kind": "plan"})
    assert plan["id"] and log.count("task.created") == 2
    assert repo.get(plan["id"])["origin"] == T.USER                  # порядок list() при равном времени не гарантирован
    assert r.get("нет") is None


async def test_impulse_timer_reads_due_dates_from_tasks():
    clock, events = Clock(), []

    async def sink(e):
        events.append(e)

    svc = service(clock=clock)
    svc.reminders = TaskReminders(svc.repo, clock=clock)
    timer = ImpulseTimer(svc.reminders, sink, clock=clock)
    await timer.add(NOW + 30, "позвонить", source={"kind": "conversation"})
    assert await timer.fire_due() == 0
    clock.t = NOW + 31
    assert await timer.fire_due() == 1
    assert "позвонить" in events[0]["text"] and svc.reminders.pending() == []
    assert (await svc.list_tasks("*", only_open=False))[0].status == T.DONE


# ── перенос reminders.json ───────────────────────────────────────────────────

def test_migrate_reminders_once_without_loss(tmp_path):
    old = [
        {"id": "r-aa", "due": NOW + 60, "text": "старое", "source": {"kind": "plan"}, "status": "pending", "depth": 2,
         "untrusted": True, "created": NOW - 5, "ttl_s": None},
        {"id": "r-bb", "due": NOW - 60, "text": "прошлое", "source": {}, "status": "fired", "depth": 0, "untrusted": False, "created": NOW - 90},
        {"id": "r-cc", "text": "без срока"},
    ]
    (tmp_path / "reminders.json").write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    store = TaskStore(lambda: tmp_path / "tasks.db", on_open=migrate_reminders)
    rem = TaskReminders(store, clock=Clock())
    got = rem.pending()
    assert [r["id"] for r in got] == ["r-aa"] and got[0]["depth"] == 2 and got[0]["untrusted"] and got[0]["source"] == {"kind": "plan"}
    assert rem.get("r-bb")["status"] == "fired" and store.get("r-cc") is None
    assert (tmp_path / "reminders.json").exists()               # файл не удалён
    store.close()
    store2 = TaskStore(lambda: tmp_path / "tasks.db", on_open=migrate_reminders)     # повторно не переносит
    assert migrate_reminders(store2, tmp_path) == 0 and len(store2.list(kind=T.KIND_REMINDER)) == 2


def test_migrate_without_file_marks_done(tmp_path):
    store = TaskStore(lambda: tmp_path / "tasks.db", on_open=migrate_reminders)
    assert store.list() == [] and store.get_meta("migrated_reminders_json")["count"] == 0


# ── планы пользователя ───────────────────────────────────────────────────────

async def test_plan_nudger_mirrors_plans_as_tasks_and_sets_reminder():
    clock = Clock()
    svc = service(clock=clock)
    svc.reminders = TaskReminders(svc.repo, clock=clock)
    timer = ImpulseTimer(svc.reminders, lambda e: None, clock=clock)
    plan = await svc.upsert_plan("p1", "курс", NOW + 7200)
    nudger = PlanNudger(svc, timer.add, clock=clock)
    assert await nudger.sync() == 1 and await nudger.sync() == 0       # второй просмотр ничего нового: план отмечен
    rec = svc.reminders.pending()[0]
    assert rec["due"] == NOW + 7200 - 1800 and rec["untrusted"] and rec["source"]["plan_id"] == plan.id


# ── инструменты ──────────────────────────────────────────────────────────────

async def test_task_tools_list_create_close(tmp_path):
    from user_memory import UserMemory
    svc = service(FakePlanner(["шаг один", "шаг два"]))
    companion = SimpleNamespace(tasks=svc)
    reg = Registry()
    builtin.register(reg, companion, UserMemory(str(tmp_path)))
    tasks, create, close = reg.get("assistant.tasks"), reg.get("assistant.create_task"), reg.get("assistant.close_task")
    assert "нет" in await tasks.call({})
    out = await create.call({"goal": "собрать отчёт", "in_minutes": 60, "priority": 4})
    assert "собрать отчёт" in out and "шаги 0/2" in out and "дальше: шаг один" in out and "срок" in out
    tid = (await svc.list_tasks())[0].id
    assert tid in await tasks.call({})
    assert "закрыта (done)" in await close.call({"id": tid})
    assert "нет" in await tasks.call({}) and tid in await tasks.call({"all": True})
    with pytest.raises(ToolError):
        await close.call({"id": tid, "status": "cancelled"})
    with pytest.raises(ToolError):
        await create.call({"goal": ""})
    with pytest.raises(ToolError):
        await create.call({"goal": "x", "in_minutes": "скоро"})
    assert create.risk == "auto" and not tasks.risk == "ask"


async def test_task_tools_without_service(tmp_path):
    from user_memory import UserMemory
    reg = Registry()
    builtin.register(reg, SimpleNamespace(), UserMemory(str(tmp_path)))
    with pytest.raises(ToolError):
        await reg.get("assistant.tasks").call({})
