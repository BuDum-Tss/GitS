"""Контекст реплики: запрос к модели привязывается к тексту фразы и отдаётся дашборду (GET /api/reply_context)."""
import asyncio
import time

import pytest
from aiohttp.test_utils import make_mocked_request

import reply_trace
from dashboard import routes_memory


@pytest.fixture(autouse=True)
def _clean():
    reply_trace.clear()
    yield
    reply_trace.clear()


def test_attach_binds_pending_request_to_reply_text():
    reply_trace.begin("converse", "система", "контекст\n\nПользователь говорит: привет", max_tokens=90)
    assert reply_trace.attach("Привет.")
    e = reply_trace.find("Привет.")
    assert e["system"] == "система" and "привет" in e["user"] and e["kind"] == "converse" and e["params"]["max_tokens"] == 90
    assert not reply_trace.attach("Ещё реплика")        # запрос уже использован: чужой реплике не достаётся


def test_no_request_or_expired_request_gives_nothing():
    assert not reply_trace.attach("Без модели")
    reply_trace.begin("fact", "s", "u")
    reply_trace._pending["t"] = time.time() - reply_trace.PENDING_TTL - 1
    assert not reply_trace.attach("Поздно")
    reply_trace.begin("fact", "s", "u")
    reply_trace.drop()
    assert not reply_trace.attach("Отброшено")
    assert reply_trace.find("Поздно") is None


def test_find_prefers_latest_and_tolerates_partial_text():
    reply_trace.begin("converse", "s1", "u1"); reply_trace.attach("Да.")
    reply_trace.begin("converse", "s2", "u2"); reply_trace.attach("Да.")
    assert reply_trace.find("Да.")["system"] == "s2"
    reply_trace.begin("converse", "s3", "u3"); reply_trace.attach("Первое. Второе.")
    assert reply_trace.find("Первое.")["system"] == "s3"


def test_ring_is_bounded():
    for i in range(reply_trace.RING + 20):
        reply_trace.begin("converse", "s", "u")
        reply_trace.attach(f"реплика {i}")
    assert len(reply_trace.recent(1000)) == reply_trace.RING
    assert reply_trace.find("реплика 0") is None


def test_api_returns_context_or_not_found():
    reply_trace.begin("converse", "sys", "usr")
    reply_trace.attach("Готово.")

    async def call(text):
        req = make_mocked_request("GET", "/api/reply_context", match_info={})
        req = req.clone(rel_url="/api/reply_context?text=" + text)
        import json
        return json.loads((await routes_memory.handle_api_reply_context(req)).body)

    got = asyncio.run(call("Готово."))
    assert got["found"] and got["system"] == "sys" and got["user"] == "usr"
    assert asyncio.run(call("Неизвестно")) == {"found": False}
