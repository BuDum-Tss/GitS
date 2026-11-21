"""Единый агент: сообщение пользователя идёт в одну сессию, а не в классификатор, планировщик и быструю модель."""
import asyncio

from services.conversation import resident
from interfaces.contracts import TextEvent
from resident_helpers import FakeHost, FakeRuntime


def _agent(script=None):
    rt = FakeRuntime(script=script if script is not None else [TextEvent("Сейчас запущу игру."), TextEvent("Запустила.")])
    host = FakeHost()
    return resident.ResidentAgent(host, runtime=rt), host, rt


def test_user_message_goes_to_one_session_and_is_spoken_as_it_comes():
    agent, host, rt = _agent()
    asyncio.run(agent.handle_user("запусти игру"))
    assert host.said == ["Сейчас запущу игру.", "Запустила."]               # текст между вызовами звучит сразу
    assert "Пользователь говорит: запусти игру" in rt.prompts[0]
    asyncio.run(agent.handle_event("inbox.batch", "что-то случилось"))
    assert len(rt.prompts) == 2                                              # события в той же сессии


def test_wait_speech_returns_when_queue_is_empty_and_gives_up_after_limit():
    """Инструмент ждёт, пока договорит очередь речи, но не дольше предела."""
    import time
    from services.conversation.companion import Companion

    class Queue:
        def __init__(self, items, busy):
            self.items, self._busy = items, busy

        def busy(self):
            return self._busy

    class Fake:
        pass

    async def run(queue, limit):
        fake = Fake()
        fake.attention = type("A", (), {"speech": queue})()
        t = time.monotonic()
        await Companion.wait_speech(fake, limit)
        return time.monotonic() - t

    assert asyncio.run(run(Queue([], False), 5)) < 0.2             # говорить нечего: не ждём
    assert 0.4 < asyncio.run(run(Queue([], True), 0.5)) < 1.5      # всё ещё звучит: ждём до предела


def test_turn_stages_are_marked_in_the_latency_trace():
    from services.tracing import latency_trace
    agent, host, _ = _agent()
    t = latency_trace.start("dashboard")
    token = latency_trace.bind(t)
    try:
        asyncio.run(agent.handle_user("привет"))
    finally:
        latency_trace.unbind(token)
    assert {"core_in", "route", "llm_first_token", "first_sentence_ready", "llm_done"} <= set(t.marks)


def test_important_event_interrupts_current_work_and_is_handled_first():
    agent, host, rt = _agent()
    rt.hold = True                                          # первое обращение долгое, пока его не оборвут
    agent._quiet = lambda: True                             # «не отвлекать»: тревога всё равно говорит

    async def go():
        long = asyncio.create_task(agent.handle_user("настрой всё"))
        await asyncio.sleep(0.1)
        assert agent.busy
        await asyncio.wait_for(agent.preempt("rimworld.raid", "Налёт: 6 врагов"), 5)
        await long
    asyncio.run(go())
    assert rt.aborts == 1 and len(rt.prompts) == 2
    assert "СРОЧНОЕ событие (rimworld.raid)" in rt.prompts[1] and "Налёт" in rt.prompts[1]


def test_dispatcher_hands_important_module_event_to_agent():
    from services.attention.dispatcher import Dispatcher
    got = []
    ctx = type("C", (), {"on_alert": lambda self, item: got.append(item) or True})()
    d = Dispatcher.__new__(Dispatcher)
    d.ctx = ctx
    item = type("I", (), {"origin": "external", "needs_reply": False})()
    assert d.alerted(item) is True and got == [item]
    assert d.alerted(type("I", (), {"origin": "self", "needs_reply": False})()) is False        # свой импульс не тревога
    assert d.alerted(type("I", (), {"origin": "external", "needs_reply": True})()) is False     # обращение к ней идёт через разговор
    d.ctx = type("C", (), {"on_alert": None})()
    assert d.alerted(item) is False                                                             # без единого агента путь прежний


def test_reaction_tag_becomes_sticker_and_is_not_spoken():
    agent, host, _ = _agent([TextEvent("[question] Как тебя зовут?")])
    asyncio.run(agent.handle_user("привет"))
    assert host.said == ["Как тебя зовут?"] and host.stickers == ["question"]


def test_turn_timeout_leaves_a_note_for_the_next_turn(monkeypatch):
    agent, host, rt = _agent()
    rt.delay = 5
    monkeypatch.setattr(resident, "TURN_TIMEOUT_S", 0.05)
    asyncio.run(agent.handle_user("играй"))
    assert "оборвался по времени" in agent._note and rt.aborts == 1
    rt.delay = 0
    asyncio.run(agent.handle_user("продолжай"))
    assert rt.prompts[-1].startswith("=== ЗАМЕТКА ===") and agent._note == ""


def test_actions_block_names_the_agents_own_calls_as_hers():
    from services.tracing import tool_trace
    from services.conversation.context_builder import ContextBuilder
    tool_trace.note("external", "rimworld.colony_overview")
    block = ContextBuilder.actions_block()
    assert "ты сама" in block and "colony_overview" in block and "вне планировщика" not in block


def test_montage_mode_speaks_only_the_final_text_of_the_turn():
    """В режиме монтажа реплика перед вызовом инструмента (комментарий к работе) не звучит, пустая реплика тоже."""
    from interfaces.contracts import ToolEvent
    agent, host, _ = _agent([TextEvent("Запускаю приём."), ToolEvent("call_tool", {"tool_id": "montage.run"}),
                             TextEvent("Жду."), ToolEvent("call_tool", {"tool_id": "montage.work"}), TextEvent("Ролик готов.")])
    host.workspace_of = lambda: "montage"
    asyncio.run(agent.handle_user("сделай ролик"))
    assert host.said == ["Ролик готов."]
    agent, host, _ = _agent([TextEvent("Запускаю."), ToolEvent("call_tool", {"tool_id": "montage.run"}), TextEvent("…")])
    host.workspace_of = lambda: "montage"
    asyncio.run(agent.handle_user("сделай ролик"))
    assert host.said == []
    agent, host, _ = _agent([TextEvent("Запускаю."), ToolEvent("call_tool", {"tool_id": "x"}), TextEvent("Готово.")])
    host.workspace_of = lambda: "rimworld"
    asyncio.run(agent.handle_user("привет"))
    assert host.said == ["Запускаю.", "Готово."]                             # вне монтажа поведение прежнее
