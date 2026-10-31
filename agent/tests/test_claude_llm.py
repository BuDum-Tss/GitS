"""Быстрая модель на Claude: выбор всегда (настройки `fast_llm` больше нет), ответ целиком и потоком, лимит подписки. Сети и Claude нет: `query` подменён."""
import asyncio

import pytest

import claude_llm
import fast_brain
from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock


def _fake_query(messages):
    async def query(prompt, options):
        for m in messages:
            yield m
    return query


def _assistant(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="haiku")


def test_ask_collects_text(monkeypatch):
    monkeypatch.setattr(claude_llm, "query", _fake_query([_assistant("Привет.")]))
    monkeypatch.setattr(claude_llm.claude_quota, "blocked", lambda: False)
    assert asyncio.run(claude_llm.ask("sys", "user", 5)) == "Привет."


def test_empty_answer_and_quota_are_errors(monkeypatch):
    monkeypatch.setattr(claude_llm, "query", _fake_query([]))
    monkeypatch.setattr(claude_llm.claude_quota, "blocked", lambda: False)
    with pytest.raises(RuntimeError):
        asyncio.run(claude_llm.ask("sys", "user", 5))
    monkeypatch.setattr(claude_llm.claude_quota, "blocked", lambda: True)
    with pytest.raises(RuntimeError, match="лимит"):
        asyncio.run(claude_llm.ask("sys", "user", 5))


def test_stream_yields_text_deltas(monkeypatch):
    def delta(t):
        return StreamEvent(uuid="u", session_id="s", event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})
    monkeypatch.setattr(claude_llm, "query", _fake_query([delta("Раз. "), delta("Два."), _assistant("Раз. Два.")]))
    monkeypatch.setattr(claude_llm.claude_quota, "blocked", lambda: False)

    async def collect():
        return [p async for p in claude_llm.ask_stream("sys", "user", 5)]
    assert asyncio.run(collect()) == ["Раз. ", "Два."]       # итоговое сообщение не дублирует уже отданный текст


def test_fast_brain_goes_to_claude(monkeypatch):
    calls = []

    async def fake_ask(system, user, timeout, image_b64=None, contour="fast"):
        calls.append(user)
        return "ответ"
    monkeypatch.setattr(claude_llm, "ask", fake_ask)
    assert asyncio.run(fast_brain._ask("s", "вопрос", 1)) == "ответ" and calls == ["вопрос"]
