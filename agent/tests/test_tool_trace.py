"""Журнал запусков инструментов для чата: планировщик (быстрый контур), Claude-помощник (медленный), вызовы со стороны."""
import pytest

import tool_trace
from adapters.approver import ApprovalsModule
from adapters.settings_provider import SettingsModule
from dashboard import presenters
from environments.base import ToolError
from router import Router
from test_app_attention import Rig


@pytest.fixture(autouse=True)
def _clean():
    tool_trace.clear()
    yield
    tool_trace.clear()


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer

    async def ask(self, *_a, **_k):
        return self.answer


class FakeLog:
    def log(self, *_a, **_k):
        pass


def make_tool(call):
    class T:
        id = "home.light"
        name = "light"
        risk = "auto"
        danger = None
        danger_check = False
        check = None
        self_confirming = False
        informative = False
        timeout_s = 5
        description = "включить свет"
        params = {}

        def signature(self):
            return "home.light(on) - включить свет"

        async def call(self, args):
            return await call(args)
    return T()


def make_router(tool, plan):
    class Registry:
        async def search_async(self, _text):
            return [tool]

        def describe(self):
            return ""

    return Router(Registry(), lambda _t: None, lambda: "", llm=FakeLLM(plan), event_log=FakeLog(),
                  settings_provider=SettingsModule(), approver=ApprovalsModule())


PLAN = '{"kind": "action", "feasible": true, "reason": "", "say_first": "", "steps": [{"tool": "home.light", "args": {}}]}'


async def test_planner_step_is_recorded_as_fast_executor():
    async def ok(_args):
        return "включено"
    router = make_router(make_tool(ok), PLAN)
    await router.handle("включи свет", announce=False)
    [e] = tool_trace.recent()
    assert (e["executor"], e["tool"], e["state"], e["result"]) == ("fast", "home.light", "done", "включено")


async def test_failed_step_is_recorded_with_reason():
    async def broken(_args):
        raise ToolError("лампа сломана")
    router = make_router(make_tool(broken), PLAN)
    await router.handle("включи свет", announce=False)
    [e] = tool_trace.recent()
    assert e["state"] == "failed" and "лампа сломана" in e["result"]


async def test_call_from_outside_is_external():
    async def ok(_args):
        return "ок"
    tool = make_tool(ok)
    router = make_router(tool, PLAN)
    router.registry.get = lambda tid: tool
    await router.run_tool_ex("home.light", {})
    assert [e["executor"] for e in tool_trace.recent()] == ["external"]


async def test_claude_inner_tool_stage_goes_to_chat_not_to_attention(tmp_path):
    rig = Rig(tmp_path)
    await rig.att.on_job_update("desktop", {"job_id": "j-1", "state": "running", "stage": "tool:Bash:1", "text": "ls -la"})
    await rig.att.settle()
    [e] = tool_trace.recent()
    assert (e["executor"], e["tool"], e["summary"], e["state"]) == ("claude", "Bash", "ls -la", "done")
    assert len(rig.att._items) == 0


def test_summary_is_clipped_and_ring_is_bounded():
    assert tool_trace.summarize({"a": "x" * 500}).endswith("…")
    for i in range(tool_trace.RING + 5):
        tool_trace.note("fast", f"t{i}")
    assert len(tool_trace.recent(1000)) == tool_trace.RING


def test_core_payload_carries_tools():
    payload = presenters.core_payload(dialogue=[], activity=[], screen=None, memories=[], relations=[], approval={},
                                      tools=[{"id": 1}])
    assert payload["tools"] == [{"id": 1}]
