"""Шаги задач от самого агента: assistant.create_task со steps, assistant.task_step (номер, текст, add, статусы, лимиты) и то, что
core.tasks (`TaskService.snapshot`) отдаёт статусы шагов и прогресс."""
from types import SimpleNamespace

import pytest

from environments import builtin
from environments.base import ToolError
from environments.registry import Registry
from services.tasks import model as T
from services.tasks.service import TaskError, TaskService
from services.tasks.store import TaskStore
from test.test_tasks import Clock, FakePlanner, service


def tools(tmp_path, planner=None):
    from services.memory_service.user import UserMemory
    svc = service(planner)
    reg = Registry()
    builtin.register(reg, SimpleNamespace(tasks=svc), UserMemory(str(tmp_path)))
    return svc, reg.get("assistant.create_task"), reg.get("assistant.task_step"), reg.get("assistant.close_task")


async def test_create_task_with_steps_skips_planner(tmp_path):
    planner = FakePlanner(["чужой шаг"])
    svc, create, _, _ = tools(tmp_path, planner)
    out = await create.call({"goal": "ролик", "steps": ["Сценарий", "История", "План ролика"]})
    assert "шаги 0/3" in out and "дальше: Сценарий" in out and planner.calls == []
    t = (await svc.list_tasks())[0]
    assert [s.text for s in t.steps] == ["Сценарий", "История", "План ролика"]


async def test_create_task_steps_limits(tmp_path):
    svc, create, _, _ = tools(tmp_path)
    await create.call({"goal": "много", "steps": [f"шаг {i} " + "я" * 200 for i in range(20)]})
    t = (await svc.list_tasks())[0]
    assert len(t.steps) == T.STEPS_MAX and all(len(s.text) <= 120 for s in t.steps)
    with pytest.raises(ToolError):
        await create.call({"goal": "x", "steps": 5})


async def test_task_step_by_number_and_text(tmp_path):
    svc, create, step, _ = tools(tmp_path)
    await create.call({"goal": "ролик", "steps": ["Сценарий", "История", "План ролика"]})
    tid = (await svc.list_tasks())[0].id
    out = await step.call({"id": tid, "step": "2"})
    assert out == "Шаг 2 из 3 «История»: сделан; дальше: «Сценарий»"
    out = await step.call({"id": tid, "step": "сценарий", "status": "skipped"})
    assert out == "Шаг 1 из 3 «Сценарий»: пропущен; дальше: «План ролика»"
    out = await step.call({"id": tid, "step": "План ролика", "note": "готово"})
    assert "сделан" in out and "close_task" in out and "готово" in out
    t = await svc.get(tid)
    assert t.is_open and [s.status for s in t.steps] == ["skipped", "done", "done"]      # все шаги сделаны, задача открыта
    out = await step.call({"id": tid, "step": 3, "status": "pending"})
    assert "снова в плане" in out and T.progress(await svc.get(tid)) == (2, 3)


async def test_task_step_add_and_limits(tmp_path):
    svc, create, step, close = tools(tmp_path)
    await create.call({"goal": "ролик", "steps": ["А", "Б"]})
    tid = (await svc.list_tasks())[0].id
    assert "«В»: добавлен" in await step.call({"id": tid, "add": "В"})
    assert [s.text for s in (await svc.get(tid)).steps] == ["А", "Б", "В"]
    with pytest.raises(ToolError):
        await step.call({"id": tid, "add": "а"})                       # дубль
    for i in range(9):
        await step.call({"id": tid, "add": f"доп {i}"})
    with pytest.raises(ToolError):
        await step.call({"id": tid, "add": "лишний"})                  # больше 12 нельзя
    with pytest.raises(ToolError):
        await step.call({"id": tid, "step": "99"})
    with pytest.raises(ToolError):
        await step.call({"id": tid, "step": "нет такого"})
    with pytest.raises(ToolError):
        await step.call({"id": tid})
    with pytest.raises(ToolError):
        await step.call({"id": "t-нет", "step": "1"})
    with pytest.raises(ToolError):
        await step.call({"id": tid, "step": "1", "status": "готово"})
    await close.call({"id": tid})
    with pytest.raises(ToolError):
        await step.call({"id": tid, "step": "1"})                      # закрытая задача
    assert step.risk == "auto"


async def test_core_tasks_snapshot_has_step_statuses_and_progress(tmp_path):
    svc, create, step, _ = tools(tmp_path)
    await create.call({"goal": "ролик", "steps": ["А", "Б", "В"]})
    tid = (await svc.list_tasks())[0].id
    await step.call({"id": tid, "step": "1"})
    await step.call({"id": tid, "step": "2", "status": "skipped"})
    snap = await svc.snapshot()
    for t in (snap["active"], snap["tasks"][0]):
        assert t["id"] == tid and t["progress"] == [2, 3]
        assert [s["status"] for s in t["steps"]] == ["done", "skipped", "pending"]
    assert any(kind == "task.step" for kind, _ in svc.log)


async def test_update_step_service_errors():
    svc = service()
    t = await svc.create("цель", steps=["А"])
    with pytest.raises(TaskError):
        await svc.update_step(t.id)
    done, idx = await svc.update_step(t.id, "А")
    assert idx == 0 and done.status == T.PENDING


async def test_plan_made_before_enter_moves_to_new_workspace():
    svc = service()
    plan = await svc.create("ролик", steps=["А", "Б"])
    plain = await svc.create("без шагов")
    assert plan.workspace == "assistant"
    svc.cur["ws"] = "montage"
    assert await svc.carry_plans("assistant", "montage") == 1
    snap = await svc.snapshot("montage")
    assert [t["id"] for t in snap["tasks"]] == [plan.id] and (await svc.get(plain.id)).workspace == "assistant"
    assert await svc.carry_plans("montage", "assistant") == 0
