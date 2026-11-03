"""Единый агент: сообщение пользователя идёт в одну сессию, а не в классификатор, планировщик и быструю модель."""
import asyncio

from app import resident
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


class FakeClient:
    instances: list = []

    def __init__(self, options):
        self.options = options
        self.prompts: list[str] = []
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def interrupt(self):
        pass

    async def query(self, prompt):
        self.prompts.append(prompt)

    async def receive_response(self):
        yield AssistantMessage(content=[TextBlock(text="Сейчас запущу игру.")], model="haiku")
        yield AssistantMessage(content=[TextBlock(text="Запустила.")], model="haiku")
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                            total_cost_usd=0.0, usage={}, result="")


class FakeContext:
    async def use_retrieval(self, *a, **k):
        return True

    def start_retrieval(self, text):
        return None


class FakeRegistry:
    def describe(self):
        return "окружения"

    async def search_async(self, text):
        return []


class FakeHost:
    def __init__(self):
        self.said: list[str] = []
        self.stickers: list = []
        self.router = type("R", (), {"registry": FakeRegistry()})()
        self.context_builder = FakeContext()

    async def say(self, text, *a, **k):
        self.said.append(text)
        self.stickers.append(a[0] if a else k.get("sticker"))

    def _declutter(self, text):
        return text

    def _context(self, **k):
        return "[контекст]"

    def profile_hint(self):
        return ""


def _agent(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(resident, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(resident.claude_quota, "blocked", lambda: False)
    import tempfile
    from pathlib import Path
    session_file = Path(tempfile.mkdtemp()) / "resident_session.json"
    monkeypatch.setattr(resident.ResidentAgent, "_session_file", staticmethod(lambda: session_file))
    host = FakeHost()
    return resident.ResidentAgent(host), host


def test_user_message_goes_to_one_session_and_is_spoken_as_it_comes(monkeypatch):
    agent, host = _agent(monkeypatch)
    asyncio.run(agent.handle_user("запусти игру"))
    assert host.said == ["Сейчас запущу игру.", "Запустила."]               # текст между вызовами звучит сразу
    assert "Пользователь говорит: запусти игру" in FakeClient.instances[0].prompts[0]
    asyncio.run(agent.handle_event("inbox.batch", "что-то случилось"))
    assert len(FakeClient.instances) == 1 and len(FakeClient.instances[0].prompts) == 2    # события в той же сессии


def test_wait_speech_returns_when_queue_is_empty_and_gives_up_after_limit():
    """Инструмент ждёт, пока договорит очередь речи, но не дольше предела."""
    import time
    from companion import Companion

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


def test_session_survives_restart_of_the_core(monkeypatch):
    agent, host = _agent(monkeypatch)
    asyncio.run(agent.handle_user("раз"))
    assert "resume" not in FakeClient.instances[0].options.__dict__ or not FakeClient.instances[0].options.resume
    asyncio.run(agent.reset(forget=False))                  # остановка ядра
    again = resident.ResidentAgent(host)                    # новый запуск ядра
    asyncio.run(again.handle_user("два"))
    assert FakeClient.instances[1].options.resume == "s"    # тот же разговор, а не чистый лист


def test_failure_forgets_the_saved_session(monkeypatch):
    agent, host = _agent(monkeypatch)
    asyncio.run(agent.handle_user("раз"))
    asyncio.run(agent.reset())                              # сбой или обрыв: прежняя сессия не нужна
    asyncio.run(resident.ResidentAgent(host).handle_user("два"))
    assert not FakeClient.instances[1].options.resume


def test_session_restarts_after_limit(monkeypatch):
    agent, host = _agent(monkeypatch)
    monkeypatch.setattr(resident, "MAX_QUERIES", 1)
    asyncio.run(agent.handle_user("раз"))
    asyncio.run(agent.handle_user("два"))
    assert len(FakeClient.instances) == 2


def test_important_event_interrupts_current_work_and_is_handled_first(monkeypatch):
    agent, host = _agent(monkeypatch)
    release = asyncio.Event()

    class SlowClient(FakeClient):
        async def interrupt(self):
            release.set()

        async def receive_response(self):
            if len(self.prompts) == 1:                      # первое обращение долгое, пока его не оборвут
                await release.wait()
                yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                    session_id="s", total_cost_usd=0.0, usage={}, result="")
                return
            async for m in super().receive_response():
                yield m

    monkeypatch.setattr(resident, "ClaudeSDKClient", SlowClient)
    monkeypatch.setattr(agent, "_quiet", lambda: True)      # «не отвлекать»: тревога всё равно говорит

    async def go():
        long = asyncio.create_task(agent.handle_user("настрой всё"))
        await asyncio.sleep(0.1)
        assert agent.busy
        await asyncio.wait_for(agent.preempt("rimworld.raid", "Налёт: 6 врагов"), 5)
        await long
    asyncio.run(go())
    prompts = FakeClient.instances[0].prompts
    assert len(prompts) == 2 and "СРОЧНОЕ событие (rimworld.raid)" in prompts[1] and "Налёт" in prompts[1]
    assert host.said == ["Сейчас запущу игру.", "Запустила."]


def test_dispatcher_hands_important_module_event_to_agent():
    from app.dispatcher import Dispatcher
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


def test_reaction_tag_becomes_sticker_and_is_not_spoken(monkeypatch):
    class TaggedClient(FakeClient):
        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text="[question] Как тебя зовут?")], model="haiku")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                                session_id="s", total_cost_usd=0.0, usage={}, result="")
    agent, host = _agent(monkeypatch)
    monkeypatch.setattr(resident, "ClaudeSDKClient", TaggedClient)
    asyncio.run(agent.handle_user("привет"))
    assert host.said == ["Как тебя зовут?"] and host.stickers == ["question"]


def test_session_reset_after_timeout_leaves_a_note_for_the_new_session(monkeypatch):
    class Slow(FakeClient):
        async def receive_response(self):
            await asyncio.sleep(5)
            yield
    agent, host = _agent(monkeypatch)
    monkeypatch.setattr(resident, "ClaudeSDKClient", Slow)
    monkeypatch.setattr(resident, "TURN_TIMEOUT_S", 0.05)
    asyncio.run(agent.handle_user("играй"))
    assert "оборвался по времени" in agent._note
    monkeypatch.setattr(resident, "ClaudeSDKClient", FakeClient)
    FakeClient.instances = []
    asyncio.run(agent.handle_user("продолжай"))
    assert FakeClient.instances[0].prompts[0].startswith("=== ЗАМЕТКА ===") and agent._note == ""


def test_actions_block_names_the_agents_own_calls_as_hers():
    import tool_trace
    from app.context_builder import ContextBuilder
    tool_trace.note("external", "rimworld.colony_overview")
    block = ContextBuilder.actions_block()
    assert "ты сама" in block and "colony_overview" in block and "вне планировщика" not in block
