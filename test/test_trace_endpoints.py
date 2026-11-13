"""Эндпоинты трассы задержки и журнала решений дашборда (ТЗ 24, TRC-2, TRC-5, TRC-6): только свои запросы, без процессов."""
import events_log
import journal
import latency_trace as lt
from aiohttp.test_utils import TestClient, TestServer

import dashboard


async def _client():
    return TestClient(TestServer(dashboard.build_app()))


async def test_latency_endpoint_returns_report_and_rejects_bad_window():
    c = await _client()
    await c.start_server()
    try:
        events_log.log_nowait("trace", "voice", trace_id="t1", source="voice",
                              marks={"mic_end": 0.0, "llm_done": 40.0, "play_start": 90.0}, end_to_sound_ms=90.0)
        events_log.flush()
        r = await c.get("/api/latency")
        assert r.status == 200
        body = await r.json()
        assert body["traces"] >= 1 and "steps" in body and "end_to_sound" in body
        assert body["end_to_sound"]["n"] >= 1
        r = await c.get("/api/latency", params={"window": "много"})
        assert r.status == 400
    finally:
        await c.close()


async def test_rating_bad_is_a_mistake_and_good_is_not():
    c = await _client()
    await c.start_server()
    try:
        r = await c.post("/api/rating", json={"value": "bad", "trace_id": "abc"})
        assert r.status == 200
        r = await c.post("/api/rating", json={"value": "good", "trace_id": "abc"})
        assert r.status == 200
        r = await c.post("/api/rating", json={"value": "ok"})
        assert r.status == 400
    finally:
        await c.close()
    events_log.flush()
    bad = [e for e in events_log.tail(limit=50, kinds={"mistake"}) if (e.get("data") or {}).get("type") == "bad_rating"]
    assert len(bad) == 1 and bad[0]["data"]["trace_id"] == "abc"


async def test_decisions_endpoint_filters_by_trace():
    t = lt.start("voice")
    token = lt.bind(t)
    try:
        journal.decision("task", "attention", "включи свет", confidence=0.9)
    finally:
        lt.unbind(token)
    journal.decision("chat", "conversation", "привет")
    events_log.flush()
    c = await _client()
    await c.start_server()
    try:
        r = await c.get("/api/decisions", params={"trace_id": t.id})
        rows = (await r.json())["decisions"]
        assert len(rows) == 1 and rows[0]["route"] == "task"
        r = await c.get("/api/decisions", params={"limit": "x"})
        assert r.status == 400
    finally:
        await c.close()
