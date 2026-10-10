"""Классификатор (ТЗ 03, ATT-3, ATT-4): вопросы, разбор ответов, клиент протокола и Decider. Сервис подменяется локальным
aiohttp-сервером и заглушкой; настоящая модель в тестах не участвует."""
import asyncio

import pytest
from aiohttp import web
from decide import decide as http_decide      # настоящий клиент: conftest подменяет decide.decide заглушкой

import decide
from decide import CATEGORY_URGENCY, EVENT_QUESTIONS, INPUT_QUESTIONS, Decider, parse_event, parse_input


def ans(value, conf):
    return {"type": "choice", "answer": value, "confidence": conf, "probabilities": {value: conf}}


def test_questions_match_the_protocol_and_urgency_table():
    cats = set(EVENT_QUESTIONS["category"]["options"])
    assert cats == set(CATEGORY_URGENCY)                                 # у каждой категории есть срочность
    assert CATEGORY_URGENCY["danger"] == 5 and CATEGORY_URGENCY["spam"] == 0
    assert EVENT_QUESTIONS["category"]["type"] == "choice" and EVENT_QUESTIONS["needs_reply"]["type"] == "noul"
    assert set(INPUT_QUESTIONS["intent"]["options"]) == {"chat", "action", "about_self", "approval", "attention_rule", "stop"}
    for q in (*EVENT_QUESTIONS.values(), *INPUT_QUESTIONS.values()):
        assert q["type"] in ("choice", "noul")                           # только категориальные вопросы, порядковых нет
        assert q["type"] != "choice" or all(isinstance(v, str) and v for v in q["options"].values())   # метка с описанием


def test_parse_event_uses_answers_only_above_the_confidence_threshold():
    answers = {"category": ans("danger", 0.73), "addressed": ans("assistant", 0.5), "needs_reply": ans("yes", 0.52)}
    cls = parse_event(answers, 0.35)
    assert (cls.category, cls.urgency, cls.addressed, cls.needs_reply) == ("danger", 5, True, True)
    weak = parse_event({"category": ans("danger", 0.34), "addressed": ans("assistant", 0.2),
                        "needs_reply": ans("yes", 0.1)}, 0.35)
    assert (weak.category, weak.urgency, weak.addressed, weak.needs_reply) == (None, None, None, None)
    # порог из настроек: 0.35 - граница включительно
    assert parse_event({"category": ans("news", 0.35)}, 0.35).category == "news"
    assert parse_event({"category": ans("news", 0.35)}, 0.5).category is None


def test_parse_event_addressed_values_and_garbage():
    assert parse_event({"addressed": ans("user", 0.9)}, 0.35).addressed is False
    assert parse_event({"addressed": ans("group", 0.9)}, 0.35).addressed is False
    assert parse_event({"category": ans("нет-такой-категории", 0.99)}, 0.35).category is None
    assert parse_event({"category": "не словарь"}, 0.35).category is None
    assert parse_event(None, 0.35) is None and parse_event({}, 0.35) is None


def test_parse_input_threshold_is_stricter():
    cls = parse_input({"intent": ans("stop", 0.53), "needs_memory": ans("no", 0.65)}, 0.5)
    assert (cls.intent, cls.needs_memory) == ("stop", False)
    low = parse_input({"intent": ans("action", 0.48), "needs_memory": ans("yes", 0.4)}, 0.5)
    assert low.intent is None and low.needs_memory is None              # ниже 0.5: как будто классификатора нет
    assert parse_input({"intent": ans("unknown", 0.9)}, 0.5).intent is None
    assert parse_input(None, 0.5) is None


async def test_decider_truncates_text_and_passes_timeout():
    seen = {}

    async def backend(text, questions, timeout):
        seen.update(text=text, questions=questions, timeout=timeout)
        return {"category": ans("news", 0.9)}
    d = Decider(backend)
    out = await d.classify("а" * 1000, EVENT_QUESTIONS, timeout=1.5)
    assert out["category"]["answer"] == "news"
    assert len(seen["text"]) == 600 and seen["timeout"] == 1.5 and seen["questions"] is EVENT_QUESTIONS
    assert d.last_ms is not None and d.last_ms >= 0


async def test_decider_returns_none_instead_of_raising():
    async def broken(text, questions, timeout):
        raise RuntimeError("сервис упал")

    async def empty(text, questions, timeout):
        return {}
    assert await Decider(broken).classify("текст", EVENT_QUESTIONS) is None
    assert await Decider(empty).classify("текст", EVENT_QUESTIONS) is None
    d = Decider(broken)
    assert await d.classify("   ", EVENT_QUESTIONS) is None and await d.classify("текст", {}) is None


# ── клиент протокола против локального сервера ───────────────────────────────────────────────────

class Service:
    def __init__(self, delay=0.0, status=200):
        self.delay, self.status = delay, status
        self.requests: list[dict] = []
        self._runner = None
        self.url = ""

    async def handler(self, request):
        self.requests.append(await request.json())
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status != 200:
            return web.json_response({"error": "x"}, status=self.status)
        return web.json_response({"answers": {"category": ans("spam", 0.8)}, "ms": 5})

    async def __aenter__(self):
        app = web.Application()
        app.router.add_post("/v1/systemone", self.handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}"
        return self

    async def __aexit__(self, *exc):
        await self._runner.cleanup()


@pytest.fixture
def fresh_client(monkeypatch):
    decide.forget_down()
    monkeypatch.setattr(decide, "_client", None)
    yield
    decide.forget_down()


async def test_client_posts_state_and_questions(fresh_client, monkeypatch):
    async with Service() as svc:
        monkeypatch.setattr(decide, "URL", svc.url)
        out = await http_decide("Скидка 70%", EVENT_QUESTIONS, timeout=2)
        assert out["category"]["answer"] == "spam"
        assert svc.requests == [{"state": "Скидка 70%", "questions": EVENT_QUESTIONS}]
        assert decide.available()


async def test_client_remembers_unavailability_for_30_seconds(fresh_client, monkeypatch):
    monkeypatch.setattr(decide, "URL", "http://127.0.0.1:1")           # закрытый порт
    assert await http_decide("текст", EVENT_QUESTIONS, timeout=1) is None
    assert not decide.available()
    async with Service() as svc:
        monkeypatch.setattr(decide, "URL", svc.url)
        assert await http_decide("текст", EVENT_QUESTIONS) is None and svc.requests == []   # в закрытую дверь не стучимся
        decide.forget_down()
        assert await http_decide("текст", EVENT_QUESTIONS) is not None


async def test_timeout_is_not_unavailability_for_decider(fresh_client, monkeypatch):
    async with Service(delay=0.5) as svc:
        monkeypatch.setattr(decide, "URL", svc.url)
        # Decider (через модульную функцию без подмены backend) не помечает сервис недоступным при тайм-ауте
        out = await http_decide("текст", EVENT_QUESTIONS, timeout=0.05, down_on_timeout=False)
        assert out is None and decide.available()
        out = await http_decide("текст", EVENT_QUESTIONS, timeout=0.05)
        assert out is None and not decide.available()                   # прежнее поведение клиента: тайм-аут = недоступен


async def test_client_http_error_is_none(fresh_client, monkeypatch):
    async with Service(status=500) as svc:
        monkeypatch.setattr(decide, "URL", svc.url)
        assert await http_decide("текст", EVENT_QUESTIONS) is None
