"""Среда агента «Монтаж» (ТЗ 32, WS-4): запросы workspace.enter / exit / resume и прежние имена против FakeCore."""
import pytest

from env_state import ACTIVE, IDLE, WorkspaceState
from tests.test_manager import Env, core_with_handlers

pytestmark = pytest.mark.usefixtures("fast")


def test_environment_ends_only_when_the_work_started_in_it_is_done():
    ws = WorkspaceState()
    ws.enter("собрать ролик", "t1", busy=False)
    assert not ws.finish_if_done(busy=False) and ws.active          # зашли с пустой очередью: ждём работы, не уходим
    ws.note_job()
    assert not ws.finish_if_done(busy=True)
    assert ws.finish_if_done(busy=False) and not ws.active          # работа кончилась: среда закончена
    assert ws.status() == {"active_policy": IDLE, "interruptible": True}


def test_busy_queue_at_entry_counts_as_the_environment_work_and_unsafe_stage_blocks_exit():
    ws = WorkspaceState()
    ws.enter(busy=True)
    assert ws.status("build") == {"active_policy": ACTIVE, "interruptible": False}
    assert ws.status("plan_approval")["interruptible"] is True
    ws.exit()
    assert ws.status("build")["interruptible"] is True              # вне среды выходить не из чего


@pytest.mark.parametrize("enter, exit_, resume", [("workspace.enter", "workspace.exit", "workspace.resume"),
                                                  ("policy.activate", "focus.release", "focus.resume")])
async def test_core_enters_exits_and_resumes_under_new_and_legacy_names(tmp_path, enter, exit_, resume):
    core = core_with_handlers(tmp_path)
    async with core, Env(tmp_path, core, ):
        status = lambda: core.states["montage"]["data"]["status"]            # noqa: E731
        assert status() == {"active_policy": IDLE, "interruptible": True}
        res = await core.request(enter, {"policy": "working", "goal": "собрать ролик", "task_id": "t1"})
        assert res["ok"] and res["policy"] == "working"
        await core.wait_for(lambda: status()["active_policy"] == ACTIVE, 5)
        res = await core.request(exit_, {"reason": "user", "checkpoint": True, "force": True})
        assert res["ok"] and res["checkpoint"]["goal"] == "собрать ролик"
        await core.wait_for(lambda: status()["active_policy"] == IDLE, 5)
        res = await core.request(resume, {"task_id": "t1", "policy": "working", "goal": "собрать ролик", "checkpoint": {}})
        assert res["ok"]
        await core.wait_for(lambda: status()["active_policy"] == ACTIVE, 5)
