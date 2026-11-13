"""Доступность модели единого агента: после ошибки лимита подписки агент отдыхает."""
import llm_gate


def test_available_by_default():
    llm_gate.reset()
    assert llm_gate.blocked() is False and llm_gate.note() == ""


def test_pi_rests_after_limit_error():
    llm_gate.reset()
    assert llm_gate.note_pi_error("сеть: ConnectError", now=100.0) is False and llm_gate.blocked(now=101.0) is False
    assert llm_gate.note_pi_error("OpenAI API error (429): usage limit reached", now=100.0) is True
    assert llm_gate.blocked(now=101.0) is True and llm_gate.blocked(now=100.0 + llm_gate.PI_COOLDOWN_S + 1) is False
    llm_gate.reset()
    assert llm_gate.blocked() is False
