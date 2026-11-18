"""ResidentRuntime + AttentionV2 + настоящий ResidentAgent на поддельной среде исполнения: ход пачкой, конец хода, прерывание,
доливка USER-хода, журнал действий. Без модели, сети и звука."""
import asyncio
from types import SimpleNamespace

from environments.base import Tool
from interfaces.contracts import EndEvent, TextEvent
from services.attention_v2.attention import AttentionV2
from services.attention_v2.event import USER
from services.attention_v2.eventlog import EventLog
from services.attention_v2.testing import FakeClock, default_registry, make_event
from services.conversation import resident
from services.runtime.turns import ResidentRuntime


class GateRuntime:
    """Среда исполнения со сценарием хода: шаги ("tool", id, args), ("say", текст), ("hold",). `abort` отпускает ожидание и обрывает ход."""

    can_steer = True

    def __init__(self, scripts=None):
        self.scripts = list(scripts or [])
        self.prompts: list[str] = []
        self.steered: list[str] = []
        self.aborts = 0
        self.tools: dict = {}
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self._abort = False

    async def start(self, system, tools):
        self.tools = {t.name: t for t in tools}

    async def run(self, prompt):
        self.prompts.append(prompt)
        self._abort = False
        self.gate.clear()
        self.started.set()
        for step in (self.scripts.pop(0) if self.scripts else []):
            if step[0] == "tool":
                await self.tools["call_tool"].handler({"tool_id": step[1], "args": step[2] if len(step) > 2 else {}})
            elif step[0] == "say":
                yield TextEvent(step[1])
            elif step[0] == "hold":
                await self.gate.wait()
            if self._abort:
                yield EndEvent("aborted")
                return
        yield EndEvent("ok")

    async def steer(self, text):
        self.steered.append(text)
        return True

    async def abort(self):
        self.aborts += 1
        self._abort = True
        self.gate.set()

    async def reset(self, forget=True):
        pass

    async def close(self):
        pass


def _tool(name, read_only=False, ok=True):
    async def call(args):
        if not ok:
            raise RuntimeError("сбой")
        return "готово"
    return Tool(id=f"test.{name}", env="test", name=name, description=name, call=call, read_only=read_only, risk="auto")


class Router:
    """Реестр и исполнитель инструментов хозяина: `registry.get` и `run_tool_ex` как у настоящего Router."""

    def __init__(self, tools):
        self.tools = {t.id: t for t in tools}
        self.registry = SimpleNamespace(get=self.tools.get, describe=lambda: "окружения")

    async def run_tool_ex(self, tool_id, args):
        tool = self.tools.get(tool_id)
        if tool is None:
            return False, "такого инструмента нет", "unknown"
        try:
            return True, await tool.call(args), None
        except Exception as e:
            return False, str(e), "internal"


class Host:
    def __init__(self, tools):
        self.said: list[str] = []
        self.router = Router(tools)
        self.context_builder = SimpleNamespace(use_retrieval=_async_true, start_retrieval=lambda text: None)

    async def say(self, text, *a, **k):
        self.said.append(text)

    async def wait_speech(self, max_s=10.0):
        return None

    def _declutter(self, text):
        return text

    def _context(self, **k):
        return "[контекст]"

    def profile_hint(self):
        return ""


async def _async_true(*a, **k):
    return True


async def wait_until(cond, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "условие не наступило вовремя"
        await asyncio.sleep(0.005)


class Rig:
    def __init__(self, scripts=None, focus="test"):
        self.clock = FakeClock()
        self.model = GateRuntime(scripts)
        self.host = Host([_tool("read", read_only=True), _tool("send"), _tool("broken", ok=False)])
        self.agent = resident.ResidentAgent(self.host, runtime=self.model)
        self.stopped: list[str] = []

        async def stop_speech(why):
            self.stopped.append(why)

        self.runtime = ResidentRuntime(self.agent, stop_speech=stop_speech)
        self.log = EventLog(clock=self.clock)
        self.attention = AttentionV2(default_registry(), self.log, self.runtime, self.clock, lambda: focus)
        self.runtime.attention_v2 = self.attention

    def ev(self, priority=None, **kw):
        kw.setdefault("workspace", "test")
        kw.setdefault("kind", "test.ev")
        return make_event(self.clock, priority, **kw)

    async def send(self, priority=None, **kw):
        e = self.ev(priority, **kw)
        await self.attention.process_event(e)
        return e

    async def idle(self):
        await wait_until(lambda: not self.runtime.current_batch() and not self.agent.busy)

    async def close(self):
        await self.runtime.close()
        self.attention.close()


async def test_turn_starts_with_batch_and_event_line_reaches_the_agent():
    r = Rig()
    e = await r.send(40, text="кто идёт играть")
    await wait_until(lambda: r.model.prompts)
    assert "test/ev" in r.model.prompts[0] and "кто идёт играть" in r.model.prompts[0]
    assert "[контекст]" in r.model.prompts[0]
    await r.idle()
    assert r.runtime.current_batch() == [] and e.state.value == "delivered"
    await r.close()


async def test_turn_end_gives_attention_on_turn_and_next_batch_starts_from_queue():
    r = Rig([[("hold",)], []])
    await r.send(40, text="первое")
    await wait_until(lambda: r.model.prompts)
    await r.send(35, text="второе")                          # 35 не превосходит занятый ход на запас: встаёт в очередь
    assert len(r.model.prompts) == 1 and len(r.attention.queue) == 1
    r.model.gate.set()
    await wait_until(lambda: len(r.model.prompts) == 2)
    assert "второе" in r.model.prompts[1] and "первое" not in r.model.prompts[1]
    await r.idle()
    assert len(r.attention.queue) == 0
    await r.close()


async def test_interrupt_without_effects_returns_events_to_queue_and_they_come_back_marked():
    r = Rig([[("tool", "test.read"), ("hold",)], [], []])
    old = await r.send(40, text="старое")
    await wait_until(lambda: r.model.prompts and r.agent.turn_actions)
    new = await r.send(90, text="срочное")                    # 90 > 40 + 20: прерывает
    await wait_until(lambda: len(r.model.prompts) == 3)       # новый ход, потом вернувшееся событие
    assert r.model.aborts == 1 and r.stopped == ["interrupted"]
    assert "срочное" in r.model.prompts[1] and "(прервано" not in r.model.prompts[1]
    assert old.interrupted is True and "⟲" in r.model.prompts[2] and "старое" in r.model.prompts[2]
    assert new.state.value == "delivered"
    await r.idle()
    await r.close()


async def test_interrupt_after_effect_does_not_requeue_and_next_prompt_has_interrupted_block():
    r = Rig([[("tool", "test.send", {"text": "Секунду"}), ("hold",)], []])
    old = await r.send(40, text="старое")
    await wait_until(lambda: any(a.status == "done" for a in r.agent.turn_actions))
    await r.send(90, text="срочное")
    await wait_until(lambda: len(r.model.prompts) == 2)
    await r.idle()
    prompt = r.model.prompts[1]
    assert "(прервано:" in prompt and "успели: test.send «Секунду»" in prompt and "старое" in prompt
    assert len(r.model.prompts) == 2 and len(r.attention.queue) == 0        # старое событие не вернулось
    assert old.interrupted is True and r.runtime.notes and "Секунду" in r.runtime.notes[0]
    await r.close()


async def test_spoken_text_counts_as_effect_for_interrupt():
    r = Rig([[("say", "Привет"), ("hold",)], []])
    await r.send(40, text="старое")
    await wait_until(lambda: r.host.said)
    await r.send(90, text="срочное")
    await wait_until(lambda: len(r.model.prompts) == 2)
    assert "успели: say «Привет»" in r.model.prompts[1]
    await r.idle()
    await r.close()


async def test_user_turn_is_not_interrupted_and_same_group_continuation_is_steered():
    r = Rig([[("hold",)], []])
    first = await r.send(USER, text="привет", group_key="chan:u1")
    await r.clock.advance(1)                                  # окно слияния COALESCE_MS
    await wait_until(lambda: r.model.prompts)
    await r.send(USER, text="и ещё вот что", group_key="chan:u1")
    assert r.model.steered and "и ещё вот что" in r.model.steered[0] and "[событие" not in r.model.steered[0]
    assert len(r.runtime.current_batch()) == 2 and r.model.aborts == 0
    other = await r.send(USER, text="я другой", group_key="chan:u2")      # другой человек ждёт в очереди, ход не прерывается
    assert r.model.aborts == 0 and other in r.attention.queue
    assert first.state.value == "delivered"
    r.model.gate.set()
    await r.clock.advance(1)
    await wait_until(lambda: len(r.model.prompts) == 2)
    await r.idle()
    await r.close()


async def test_user_batch_prompt_uses_user_source_and_asks_to_answer():
    r = Rig()
    await r.send(USER, text="ты меня слышишь")
    await wait_until(lambda: r.model.prompts)
    assert "ответь по делу" in r.model.prompts[0]
    await r.idle()
    await r.close()


async def test_action_journal_tells_read_only_from_effect_and_resets_each_turn():
    r = Rig([[("tool", "test.read"), ("tool", "test.send", {"text": "привет"}), ("tool", "test.broken"), ("tool", "test.нет")], []])
    await r.agent.run_batch("событие", "event")
    acts = {a.tool: a for a in r.agent.turn_actions}
    assert acts["test.read"].effect is False and acts["test.read"].status == "done"
    assert acts["test.send"].effect is True and acts["test.send"].status == "done" and acts["test.send"].summary == "привет"
    assert acts["test.broken"].effect is True and acts["test.broken"].status == "failed"
    assert acts["test.нет"].effect is False and acts["test.нет"].status == "failed"
    assert all(a.started_at for a in acts.values())
    await r.agent.run_batch("ещё", "event")
    assert r.agent.turn_actions == []                          # журнал сброшен в начале хода


async def test_turn_end_hook_runs_after_busy_is_reset_and_accepts_list():
    model = GateRuntime()
    agent = resident.ResidentAgent(Host([]), runtime=model)
    seen = []
    agent.on_turn_end = [lambda: seen.append(("a", agent.busy)), _async_hook(seen, agent)]
    await agent.run_batch("x", "event")
    assert seen == [("a", False), ("b", False)]


def _async_hook(seen, agent):
    async def hook():
        seen.append(("b", agent.busy))
    return hook


async def test_run_batch_keeps_handle_event_prompt_format():
    model = GateRuntime()
    agent = resident.ResidentAgent(Host([]), runtime=model)
    await agent.handle_event("inbox.batch", "пришло письмо", "telegram")
    assert model.prompts[0].startswith("=== ВХОД: событие (inbox.batch, telegram) ===\n[контекст]\n\nпришло письмо\n\n")
    assert agent.can_steer is True


async def test_foreign_turn_end_wakes_attention_queue():
    r = Rig([[("hold",)]])
    task = asyncio.create_task(r.agent.run_batch("чужой ход", "event"))         # ход, начатый не через ResidentRuntime
    await wait_until(lambda: r.model.prompts)
    await r.send(50, text="ждёт")                                             # внимание считает агента свободным и запускает ход
    assert r.runtime.current_batch()
    r.model.gate.set()
    await task
    await wait_until(lambda: len(r.model.prompts) == 2)
    await r.idle()
    await r.close()
