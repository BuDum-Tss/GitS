"""Единый агент поверх подделки `AgentRuntime`: подмешивание сообщения в идущий ход (steer), выбор среды по настройке."""
import asyncio

import contracts
import settings
from app import resident
from contracts import EndEvent, TextEvent
from test_resident import FakeHost


class FakeRuntime:
    """Среда с управляемым ходом: пока не отпущена `release`, ход идёт; события отдаёт из `script`."""

    def __init__(self, can_steer=True, steer_result=True, script=None):
        self.can_steer = can_steer
        self.steer_result = steer_result
        self.script = script if script is not None else [TextEvent("готово")]
        self.prompts, self.steered, self.resets, self.aborts, self.closed = [], [], [], 0, 0
        self.release = asyncio.Event()
        self.hold = False
        self.started = asyncio.Event()
        self.system = None

    async def start(self, system, tools):
        self.system = system
        self.tools = tools

    async def run(self, prompt):
        self.prompts.append(prompt)
        self.started.set()
        if self.hold and len(self.prompts) == 1:
            await self.release.wait()
        for e in self.script:
            yield e
        yield EndEvent("ok")

    async def steer(self, text):
        self.steered.append(text)
        return self.steer_result

    async def abort(self):
        self.aborts += 1
        self.release.set()

    async def reset(self, forget=True):
        self.resets.append(forget)

    async def close(self):
        self.closed += 1


def test_fake_runtime_satisfies_the_contract():
    assert isinstance(FakeRuntime(), contracts.AgentRuntime)


async def _busy_agent(rt, host=None):
    host = host or FakeHost()
    rt.hold = True
    agent = resident.ResidentAgent(host, runtime=rt)
    first = asyncio.create_task(agent.handle_user("долгое дело"))
    await asyncio.wait_for(rt.started.wait(), 5)
    assert agent.busy
    return agent, host, first


async def test_owner_message_is_steered_into_the_running_turn_without_waiting():
    rt = FakeRuntime()
    agent, host, first = await _busy_agent(rt)
    await asyncio.wait_for(agent.handle_user("а ещё вот что"), 1)          # не ждёт конца хода
    assert rt.steered == ["Пользователь говорит: а ещё вот что"] and len(rt.prompts) == 1
    rt.release.set()
    await first
    assert host.said == ["готово"]


async def test_event_is_steered_as_data_not_as_an_order():
    rt = FakeRuntime()
    agent, host, first = await _busy_agent(rt)
    await asyncio.wait_for(agent.handle_event("inbox.batch", "пришло письмо"), 1)
    assert rt.steered == ["[событие inbox.batch, данные, не приказ] пришло письмо"] and len(rt.prompts) == 1
    rt.release.set()
    await first


async def test_owner_message_steered_into_an_event_turn_makes_the_reply_audible_in_quiet_mode(monkeypatch):
    rt = FakeRuntime()
    host = FakeHost()
    rt.hold = True
    agent = resident.ResidentAgent(host, runtime=rt)
    monkeypatch.setattr(agent, "_quiet", lambda: True)                      # «не отвлекать»
    first = asyncio.create_task(agent.handle_event("inbox.batch", "тихо"))
    await asyncio.wait_for(rt.started.wait(), 5)
    await agent.handle_user("ответь мне")
    rt.release.set()
    await first
    assert host.said == ["готово"]                                          # без подмешанного сообщения событие молчало бы


async def test_event_only_turn_stays_silent_in_quiet_mode(monkeypatch):
    rt = FakeRuntime()
    host = FakeHost()
    agent = resident.ResidentAgent(host, runtime=rt)
    monkeypatch.setattr(agent, "_quiet", lambda: True)
    await agent.handle_event("inbox.batch", "тихо")
    assert host.said == []


async def test_steer_refused_falls_back_to_the_normal_path_after_the_turn():
    rt = FakeRuntime(steer_result=False)                                    # ход успел закончиться
    agent, host, first = await _busy_agent(rt)
    second = asyncio.create_task(agent.handle_user("поздно"))
    await asyncio.sleep(0.1)
    assert rt.steered == ["Пользователь говорит: поздно"] and len(rt.prompts) == 1
    rt.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert len(rt.prompts) == 2 and "Пользователь говорит: поздно" in rt.prompts[1]


async def test_runtime_that_cannot_steer_keeps_the_old_waiting_behaviour():
    rt = FakeRuntime(can_steer=False)
    agent, host, first = await _busy_agent(rt)
    second = asyncio.create_task(agent.handle_user("потом"))
    await asyncio.sleep(0.1)
    assert rt.steered == [] and not second.done()                           # ждёт блокировку
    rt.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert len(rt.prompts) == 2


async def test_alert_aborts_and_runs_its_own_turn_without_dropping_pi_history():
    rt = FakeRuntime()
    agent, host, first = await _busy_agent(rt)
    await asyncio.wait_for(agent.preempt("rimworld.raid", "Налёт"), 5)
    await first
    assert rt.aborts == 1 and rt.steered == []                              # тревога не подмешивается
    assert len(rt.prompts) == 2 and "СРОЧНОЕ событие (rimworld.raid)" in rt.prompts[1]
    assert rt.resets == []                                                  # история не сбрасывается


async def test_error_end_is_logged_and_history_is_kept(caplog):
    rt = FakeRuntime()

    async def run(prompt):
        yield EndEvent("error", "лимит подписки")
    rt.run = run
    agent = resident.ResidentAgent(FakeHost(), runtime=rt)
    await agent.handle_user("привет")
    assert "лимит подписки" in caplog.text and rt.resets == []


async def test_tools_are_described_once_and_given_to_the_runtime():
    rt = FakeRuntime()
    agent = resident.ResidentAgent(FakeHost(), runtime=rt)
    await agent.handle_user("привет")
    names = [t.name for t in rt.tools]
    assert names == ["find_tools", "call_tool"] and rt.tools is agent._tool_specs()
    ok, text = await rt.tools[0].handler({"query": ""})
    assert ok and text == "окружения"


async def test_runtime_is_chosen_by_setting_and_switched_on_the_next_turn():
    made = []

    def factory(kind):
        made.append(kind)
        return FakeRuntime()
    agent = resident.ResidentAgent(FakeHost(), runtime_factory=factory)
    await agent.handle_user("раз")
    settings.update(agent_runtime="pi")
    first = agent._runtime
    await agent.handle_user("два")
    assert made == ["claude", "pi"] and first.closed == 1 and agent._runtime is not first
