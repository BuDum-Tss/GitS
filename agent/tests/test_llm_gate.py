"""Доступность модели единого агента по настройке `agent_runtime`: на pi свой отдых после ошибки лимита, прежний лимит Claude не мешает."""
import claude_quota
import llm_gate
import settings


def _runtime(monkeypatch, kind):
    monkeypatch.setattr(settings, "load", lambda: {"agent_runtime": kind})
    llm_gate.reset()


def test_pi_ignores_claude_limit(monkeypatch):
    _runtime(monkeypatch, "pi")
    monkeypatch.setattr(claude_quota, "blocked", lambda: True)
    assert llm_gate.blocked() is False and llm_gate.note() == ""


def test_pi_rests_after_limit_error(monkeypatch):
    _runtime(monkeypatch, "pi")
    assert llm_gate.note_pi_error("сеть: ConnectError", now=100.0) is False and llm_gate.blocked(now=101.0) is False
    assert llm_gate.note_pi_error("OpenAI API error (429): usage limit reached", now=100.0) is True
    assert llm_gate.blocked(now=101.0) is True and llm_gate.blocked(now=100.0 + llm_gate.PI_COOLDOWN_S + 1) is False


def test_claude_uses_claude_quota(monkeypatch):
    _runtime(monkeypatch, "claude")
    monkeypatch.setattr(claude_quota, "blocked", lambda: True)
    monkeypatch.setattr(claude_quota, "until_text", lambda: "21:50")
    assert llm_gate.blocked() is True and "21:50" in llm_gate.note()
    llm_gate.note_pi_error("usage limit", now=1.0)              # отдых pi на Claude ничего не значит
    monkeypatch.setattr(claude_quota, "blocked", lambda: False)
    assert llm_gate.blocked() is False


def test_module_usage_on_pi_does_not_drain_claude_battery(monkeypatch):
    import asyncio
    import claude_battery
    from core_requests import CoreRequests
    recorded = []
    monkeypatch.setattr(claude_battery, "record", lambda tokens, source: recorded.append((tokens, source)))
    conn = type("C", (), {"module_id": "desktop"})()
    _runtime(monkeypatch, "pi")
    assert asyncio.run(CoreRequests._claude_usage(None, conn, {"tokens": 500})) == {} and recorded == []
    _runtime(monkeypatch, "claude")
    asyncio.run(CoreRequests._claude_usage(None, conn, {"tokens": 500}))
    assert recorded == [(500, "desktop")]
