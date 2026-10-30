"""Фоновые проходы (планирование, цели) говорят вслух, а вызовы их инструментов уходят в чат ядра."""
import glados_agent
from test_policy_controller import ctrl  # noqa: F401  (фикстура контроллера без игры)


class FakeLink:
    online = True

    def __init__(self):
        self.said: list[str] = []
        self.facts: list[str] = []
        self.tools: list[tuple] = []
        self.module_settings: dict = {}

    async def say(self, text):
        self.said.append(text)

    async def say_fact(self, fact):
        self.facts.append(fact)

    async def tool_call(self, name, summary=""):
        self.tools.append((name, summary))


RESULT = {"contour": "planning", "actions": ["a"], "notes": ["Нужны грядки: еды на неделю."],
          "trace": [{"name": "set_task", "summary": "поставить грядки у реки", "result": "OK", "error": False},
                    {"name": "set_task", "summary": "стена", "result": "ERROR: нет места", "error": True}]}


async def test_background_commentary_is_spoken_by_claude_itself(ctrl):  # noqa: F811
    """Один агент: реплику пишет тот же Claude, что планирует; отдельной озвучки фактами нет."""
    ctrl.link = FakeLink()
    ctrl._speak_background({**RESULT, "commentary": "Грядки у реки: еды на неделю нет."})
    ctrl._speak_background({"commentary": "Лимит", "quota": True, **RESULT})        # лимит Claude: тишина
    ctrl._speak_background({"contour": "planning", "actions": [], "trace": [], "notes": [], "commentary": ""})
    await __import__("asyncio").sleep(0)
    assert ctrl.link.said == ["Грядки у реки: еды на неделю нет."]
    assert ctrl.link.facts == []


async def test_tool_hook_reaches_the_link(ctrl):  # noqa: F811
    ctrl.link = FakeLink()
    glados_agent._notify_tool("set_task", {"description": "поставить  грядки"})
    await __import__("asyncio").sleep(0)
    assert ctrl.link.tools == [("set_task", "поставить грядки")]


def test_tool_summary_picks_a_readable_field():
    assert glados_agent.tool_summary({"goal_id": 3}) == ""
    assert glados_agent.tool_summary(None) == ""
    assert glados_agent.tool_summary({"title": "Стена"}) == "Стена"
