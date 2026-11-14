"""GET /api/tasks/now: текущая задача и шаги по всем средам для вкладки «Состояние»."""
import json
from types import SimpleNamespace

from http_api import routes_memory
from http_api.context import ctx
from services.tasks.service import TaskService
from services.tasks.store import TaskStore


async def _call():
    resp = await routes_memory.handle_api_tasks_now(SimpleNamespace())
    return json.loads(resp.body)


def _service(ws="game"):
    return TaskService(TaskStore(":memory:"), workspace_of=lambda: ws)


async def test_tasks_now_without_service_is_empty(monkeypatch):
    monkeypatch.setattr(ctx, "tasks", None)
    assert await _call() == {"active": None, "open": []}


async def test_tasks_now_empty_service(monkeypatch):
    svc = _service()
    monkeypatch.setattr(ctx, "tasks", lambda: svc)
    assert await _call() == {"active": None, "open": [], "workspace": "game"}


async def test_tasks_now_sees_other_workspace_and_prefers_active(monkeypatch):
    svc = _service("game")
    monkeypatch.setattr(ctx, "tasks", lambda: svc)
    await svc.create("поставить стену", workspace="game", priority=5)
    mon = await svc.create("смонтировать ролик", workspace="montage", steps=["нарезка", "звук"], priority=1)
    await svc.activate(mon.id)                                    # идущая важнее высокого приоритета
    body = await _call()
    assert body["workspace"] == "game"
    assert body["active"]["id"] == mon.id and body["active"]["workspace"] == "montage"
    assert [s["text"] for s in body["active"]["steps"]] == ["нарезка", "звук"]
    assert body["active"]["progress"][1] == 2
    assert [t["goal"] for t in body["open"]] == ["поставить стену"]
    assert "steps" not in body["open"][0] and body["open"][0]["workspace"] == "game"


async def test_tasks_now_priority_among_pending_and_ignores_reminders_and_closed(monkeypatch):
    svc = _service("assistant")
    monkeypatch.setattr(ctx, "tasks", lambda: svc)
    low = await svc.create("низкая", workspace="a", priority=1)
    high = await svc.create("высокая", workspace="b", priority=4)
    from services.tasks import model as dom
    await svc.create("напомнить", workspace="a", priority=5, kind=dom.KIND_REMINDER)
    done = await svc.create("сделана", workspace="a", priority=5)
    await svc.close(done.id, status=dom.DONE)
    body = await _call()
    assert body["active"]["id"] == high.id
    assert [t["id"] for t in body["open"]] == [low.id]
