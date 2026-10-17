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

    async def say(self, text, emotion=None):
        self.said.append(text)

    async def say_fact(self, fact):
        self.facts.append(fact)

    async def tool_call(self, name, summary=""):
        self.tools.append((name, summary))


RESULT = {"contour": "planning", "actions": ["a"], "notes": ["Нужны грядки: еды на неделю."],
          "trace": [{"name": "set_task", "summary": "поставить грядки у реки", "result": "OK", "error": False},
                    {"name": "set_task", "summary": "стена", "result": "ERROR: нет места", "error": True}]}


async def test_background_result_goes_to_core_as_facts_not_as_claude_text(ctrl):  # noqa: F811
    ctrl.link = FakeLink()
    ctrl._speak_background(RESULT)
    ctrl._speak_background({"commentary": "Лимит", "quota": True, **RESULT})
    ctrl._speak_background({"contour": "planning", "actions": [], "trace": [], "notes": []})
    await __import__("asyncio").sleep(0)
    assert len(ctrl.link.facts) == 1
    fact = ctrl.link.facts[0]
    assert "раскладка целей" in fact and "set_task (поставить грядки у реки): итог: OK" in fact
    assert "ОШИБКА: ERROR: нет места" in fact and "Нужны грядки" in fact
    assert ctrl.link.said == []                      # Claude вслух не говорит


async def test_tool_hook_reaches_the_link(ctrl):  # noqa: F811
    ctrl.link = FakeLink()
    glados_agent._notify_tool("set_task", {"description": "поставить  грядки"})
    await __import__("asyncio").sleep(0)
    assert ctrl.link.tools == [("set_task", "поставить грядки")]


def test_tool_summary_picks_a_readable_field():
    assert glados_agent.tool_summary({"goal_id": 3}) == ""
    assert glados_agent.tool_summary(None) == ""
    assert glados_agent.tool_summary({"title": "Стена"}) == "Стена"
