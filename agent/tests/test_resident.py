"""Единый агент: сообщение пользователя идёт в одну сессию, а не в классификатор, планировщик и быструю модель."""
import asyncio

import settings
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
        self.router = type("R", (), {"registry": FakeRegistry()})()
        self.context_builder = FakeContext()

    async def say(self, text, *a, **k):
        self.said.append(text)

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
    host = FakeHost()
    return resident.ResidentAgent(host), host


def test_user_message_goes_to_one_session_and_is_spoken_as_it_comes(monkeypatch):
    agent, host = _agent(monkeypatch)
    asyncio.run(agent.handle_user("запусти игру"))
    assert host.said == ["Сейчас запущу игру.", "Запустила."]               # текст между вызовами звучит сразу
    assert "Пользователь говорит: запусти игру" in FakeClient.instances[0].prompts[0]
    asyncio.run(agent.handle_event("inbox.batch", "что-то случилось"))
    assert len(FakeClient.instances) == 1 and len(FakeClient.instances[0].prompts) == 2    # события в той же сессии


def test_enabled_follows_setting_and_router(monkeypatch):
    agent, host = _agent(monkeypatch)
    assert agent.enabled() is False                                         # в тестах по умолчанию выключен
    settings.update(resident_agent=True)
    assert agent.enabled() is True
    host.router = None
    assert agent.enabled() is False


def test_session_restarts_after_limit(monkeypatch):
    agent, host = _agent(monkeypatch)
    monkeypatch.setattr(resident, "MAX_QUERIES", 1)
    asyncio.run(agent.handle_user("раз"))
    asyncio.run(agent.handle_user("два"))
    assert len(FakeClient.instances) == 2
