"""Окружение «Монтаж» (ТЗ 32, WS-4; контракт 3.2): состояние фокуса и запросы workspace.enter / afk / exit / resume против FakeCore."""
import pytest

from env_state import WorkspaceState
from tests.test_manager import Env, core_with_handlers

pytestmark = pytest.mark.usefixtures("fast")


def test_environment_does_not_end_by_itself_when_operations_finish():
    ws = WorkspaceState()
    assert ws.status() == {"done": True, "interruptible": True}
    ws.enter("собрать ролик", "t1")
    assert ws.status() == {"done": False, "interruptible": True}          # операции кончились или нет, пока не вышла: окружение её
    ws.go_afk()
    assert ws.status()["done"] is True and ws.afk and not ws.releasing    # AFK: фокус снят, окружение за ней
    ws.enter()                                                            # возврат: цель прежняя
    assert ws.active and not ws.afk and ws.goal == "собрать ролик"
    out = ws.exit()
    assert out["ok"] and out["checkpoint"]["goal"] == "собрать ролик" and ws.releasing and not ws.active
    ws.enter("ещё ролик", "t2")                                           # повторный вход отменяет отпускание
    assert not ws.releasing and ws.goal == "ещё ролик"


async def test_core_enters_exits_and_resumes(tmp_path):
    enter, exit_, resume = "workspace.enter", "workspace.exit", "workspace.resume"
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core, ):
        status = lambda: core.states["montage"]["data"]["status"]            # noqa: E731
        assert status() == {"done": True, "interruptible": True}
        res = await core.request(enter, {"goal": "собрать ролик", "task_id": "t1"})
        assert res["ok"] and "policy" not in res
        await core.wait_for(lambda: status()["done"] is False, 5)
        res = await core.request(exit_, {"reason": "user", "checkpoint": True, "force": True})
        assert res["ok"] and res["checkpoint"]["goal"] == "собрать ролик"
        await core.wait_for(lambda: status()["done"] is True, 5)
        res = await core.request(resume, {"task_id": "t1", "goal": "собрать ролик", "checkpoint": {}})
        assert res["ok"]
        await core.wait_for(lambda: status()["done"] is False, 5)
        res = await core.request("workspace.afk", {"reason": "render"})
        assert res["ok"]
        await core.wait_for(lambda: status()["done"] is True, 5)
