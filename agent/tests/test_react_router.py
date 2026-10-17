"""ReAct быстрого контура: объявление, шаги, взгляд на результаты и следующий круг; прерывание важным событием."""
import json

import tool_trace
from adapters.approver import ApprovalsModule
from adapters.settings_provider import SettingsModule
from router import Router
from speech_queue import SpeechQueue


class Script:
    """LanguageModel-заглушка: отвечает заготовленными ответами по очереди и запоминает запросы."""

    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def ask(self, system, user, **_k):
        self.asked.append(user)
        return self.answers.pop(0)


class Log:
    def log(self, *_a, **_k):
        pass


def tool(tid, result, informative=True, calls=None):
    class T:
        id, name, risk, danger, danger_check, check = tid, tid.split(".")[1], "auto", None, False, None
        self_confirming, timeout_s, description, params = False, 5, tid, {}

        def signature(self):
            return f"{tid}() - {tid}"

        async def call(self, args):
            if calls is not None:
                calls.append(tid)
            return result
    t = T()
    t.informative = informative
    return t


def make(llm, tools, said):
    class Registry:
        async def search_async(self, _t):
            return tools

        def describe(self):
            return ""

    async def say(text):
        said.append(text)
    return Router(Registry(), say, lambda: "", llm=llm, event_log=Log(), settings_provider=SettingsModule(),
                  approver=ApprovalsModule())


def plan(steps, observe=False, say=""):
    return json.dumps({"kind": "action", "feasible": True, "reason": "", "observe": observe, "say_first": say,
                       "steps": [{"tool": s, "args": {}} for s in steps]})


async def test_react_looks_at_results_and_plans_next_round():
    tool_trace.clear()
    calls, said = [], []
    llm = Script(plan(["core.status"], observe=True, say="Сейчас посмотрю"),
                 plan(["rw.launch"], say="Игра не запущена, запускаю"))
    router = make(llm, [tool("core.status", "игра: не запущена", calls=calls), tool("rw.launch", "запуск", False, calls)], said)
    out = await router.handle("запусти игру если не запущена", announce=True)
    assert calls == ["core.status", "rw.launch"]
    assert said == ["Сейчас посмотрю", "Игра не запущена, запускаю"]            # объявление и ход рассуждения вслух
    assert "игра: не запущена" in llm.asked[1]                                   # второй круг видит результат первого
    assert "status" in out and "launch" in out                           # итог по обоим шагам
    tool_trace.clear()


async def test_react_stops_when_next_round_is_empty_and_is_bounded():
    calls, said = [], []
    llm = Script(plan(["core.status"], observe=True, say="Смотрю"), plan([]))
    router = make(llm, [tool("core.status", "всё работает", calls=calls)], said)
    await router.handle("что с модулями", announce=True)
    assert calls == ["core.status"] and said == ["Смотрю"]
    calls.clear()
    loop = Script(*[plan(["core.status"], observe=True) for _ in range(10)])
    router = make(loop, [tool("core.status", "ок", calls=calls)], [])
    await router.handle("что с модулями", announce=False)
    assert len(calls) == 3                                                       # не больше MAX_ROUNDS кругов


async def test_urgent_event_interrupts_the_chain():
    calls = []
    llm = Script(plan(["core.status", "rw.launch"], say="Смотрю"))
    router = make(llm, [tool("core.status", "ок", calls=calls), tool("rw.launch", "запуск", False, calls)], [])
    router.interrupted = lambda: len(calls) >= 1                                  # после первого шага пришло важное
    assert await router.handle("сделай", announce=False) == ""
    assert calls == ["core.status"]


def test_speech_queue_reports_urgent_waiting():
    q = SpeechQueue.__new__(SpeechQueue)
    c = lambda p, fam: type("C", (), {"priority": p, "family": fam})()
    q.items = [c(50, "module"), c(95, "conversation")]
    assert not q.urgent_waiting()
    q.items.append(c(90, "module"))
    assert q.urgent_waiting()
