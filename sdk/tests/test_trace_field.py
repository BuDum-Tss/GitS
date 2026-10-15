"""Поле `trace` события (ТЗ 24, TRC-1): необязательное, обратно совместимое, SDK не разбирает его содержимое."""
from gits_sdk import protocol as P


def _event(**extra):
    return {"kind": "voice.utterance", "text": "привет", **extra}


def test_event_without_trace_is_valid():
    assert P.validate_event(_event()) == []


def test_event_with_trace_id_is_valid_and_marks_are_not_checked():
    body = _event(trace={"id": "abc123", "source": "voice", "marks": {"mic_end": 1.5}, "anything": [1, 2]})
    assert P.validate_event(body) == []


def test_trace_without_id_is_rejected():
    assert any(p.startswith("trace:") for p in P.validate_event(_event(trace={"marks": {}})))
    assert any(p.startswith("trace:") for p in P.validate_event(_event(trace="abc")))
