"""Исправления после ревью трассы и журнала (ТЗ 24): интенсивность «Infinity», выборка решений по trace_id, секреты в
метках, id трассы из шины, оценка с не-объектом в теле, ложная ошибка маршрута без планировщика."""
import gc
import warnings

import events_log
import journal
import latency_trace as lt
import pytest
from aiohttp.test_utils import TestClient, TestServer

import dashboard
from module_hub import ModuleHub
from test_companion_attention import Rig


@pytest.fixture
def mk(tmp_path, monkeypatch):
    return lambda **kw: Rig(tmp_path, monkeypatch, **kw)


def _mistakes(kind=None):
    events_log.flush()
    rows = events_log.tail(limit=500, kinds={"mistake"})
    return [r for r in rows if kind is None or (r.get("data") or {}).get("type") == kind]


# ── интенсивность ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [float("inf"), "Infinity", "-Infinity", float("nan"), "много", None])
def test_unusable_intensity_falls_back_to_one(value):
    assert journal.clamp_intensity(value) == 1


def test_intensity_is_clamped_to_range():
    assert journal.clamp_intensity("2.9") == 2
    assert journal.clamp_intensity(9) == 3
    assert journal.clamp_intensity(-4) == 0


def test_emotion_reason_is_trace_id_when_traced_else_the_given_reason():
    events_log.flush()
    journal.emotion("looking", 1, reason="chat")
    t = lt.start("chat")
    token = lt.bind(t)
    try:
        journal.emotion("joy", 2, reason="chat")
    finally:
        lt.unbind(token)
    events_log.flush()
    rows = events_log.tail(limit=20, kinds={"emotion"})
    reasons = [(r["data"]["emotion"], r["data"]["reason"]) for r in rows]
    assert ("looking", "chat") in reasons and ("joy", t.id) in reasons


# ── секреты в журнале ───────────────────────────────────────────────────────────

def test_tokens_and_keys_are_not_written_to_the_journal():
    journal.mistake("tool_failed", "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PwxyzABC/sendMessage"
                    " ошибка", code="internal")
    journal.mistake("tool_failed", "запрос отклонён: api_key=SECRETVALUE123 и пароль: hunter2", code="internal")
    rows = _mistakes("tool_failed")
    blob = " ".join(str(r) for r in rows)
    assert len(rows) == 2
    for secret in ("AAHdqTcvCH1vGWJxfSeofSAs0K5PwxyzABC", "SECRETVALUE123", "hunter2"):
        assert secret not in blob
    assert "[токен]" in blob and "[скрыто]" in blob


# ── выборка решений по trace_id ─────────────────────────────────────────────────

def test_decisions_by_trace_finds_an_old_row_behind_many_newer_ones():
    t = lt.start("voice")
    token = lt.bind(t)
    try:
        journal.decision("task", "attention", "включи свет", confidence=0.9)
    finally:
        lt.unbind(token)
    for i in range(120):
        journal.decision("chat", "conversation", f"привет {i}")
    events_log.flush()
    rows = journal.decisions(limit=10, trace_id=t.id)
    assert len(rows) == 1 and rows[0]["route"] == "task"
    assert len(journal.decisions(limit=10)) == 10          # без trace_id выборка по-прежнему ограничена limit


# ── id трассы из шины ───────────────────────────────────────────────────────────

def test_long_bus_id_is_found_by_the_same_key_everywhere():
    wire = lt.wire_new("voice", marks={"mic_end": 1.0})
    wire["id"] = "x" * 50                                  # длиннее обрезки 40 символов
    t = lt.from_bus(wire, "voice.utterance")
    try:
        assert t is not None and t.id == "x" * 40
        assert lt.from_bus(wire, "voice.utterance") is t    # повторное событие находит ту же трассу
        lt.audio_arrived({"id": wire["id"], "marks": {"play_start": 5.0}})
        assert t.audio is True                              # метки звука нашли трассу
    finally:
        lt._open.pop(t.id, None)


# ── оценка с телом не-объектом ──────────────────────────────────────────────────

async def test_rating_with_a_list_body_is_a_bad_request():
    c = TestClient(TestServer(dashboard.build_app()))
    await c.start_server()
    try:
        r = await c.post("/api/rating", json=[1, 2])
        assert r.status == 400
        r = await c.post("/api/rating", json="bad")
        assert r.status == 400
    finally:
        await c.close()


# ── маршрут без планировщика ────────────────────────────────────────────────────

def test_notify_without_event_loop_does_not_leave_a_coroutine_unawaited():
    # синхронный вызов без цикла событий (старт, тесты): корутина рассылки закрывается, а не теряется с предупреждением
    hub = object.__new__(ModuleHub)
    hub._tasks = set()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        hub.notify("presence", {})
        gc.collect()
    assert not [w for w in caught if "never awaited" in str(w.message)]


def test_action_intent_without_router_is_not_a_misroute(mk):
    rig = mk()
    rig.companion.router = None
    rig.companion._log_route("включи свет", "chat", False, "action", None)
    assert _mistakes("misroute") == []
