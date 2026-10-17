"""ТЗ 26: эмоция персонажа. Одна эмоция из стандартного набора, её выбирает классификатор, новая перебивает старую, держится
`emotion_ttl_s` секунд. Часы управляемые, модель и сеть не нужны."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import decide
import fast_brain
from app.affect_service import AffectService
from app.context_builder import ContextBuilder
from app.event_classifier import EventClassifier
from domain import affect as dom


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def svc(clock=None, ttl=3.0, on_change=None):
    return AffectService(lambda: ttl, clock=clock or Clock(), on_change=on_change)


def test_label_sets_are_the_same_everywhere():
    assert set(dom.LABELS) == set(fast_brain.EMOTIONS) == set(decide.EMOTION_OPTIONS)


def test_new_emotion_replaces_the_old_one_and_expires():
    c = Clock()
    a = svc(c, ttl=3.0)
    assert a.current() == "idle" and a.emotion_block() == ""
    a.set("angry", "chat")
    assert a.current() == "angry"
    c.t += 1
    a.set("happy", "event:win")                     # новая сразу перебивает старую, срок отсчитывается заново
    assert a.current() == "happy" and a.snapshot()["reason"] == "event:win"
    c.t += 2.5
    assert a.current() == "happy"
    c.t += 1
    assert a.current() == "idle" and a.emotion_block() == "" and a.snapshot()["reason"] == ""


def test_only_standard_non_idle_labels_count():
    c = Clock()
    a = svc(c)
    a.set("angry", "chat")
    for bad in ("idle", "furious", None, 5, ""):
        a.set(bad, "x")
    assert a.current() == "angry"                   # мусор и idle прежнюю эмоцию не трогают


def test_ttl_comes_from_the_setting_and_is_clamped():
    c = Clock()
    ttl = {"v": 10.0}
    a = AffectService(lambda: ttl["v"], clock=c)
    a.set("sad", "chat")
    c.t += 8
    assert a.current() == "sad"
    ttl["v"] = 5.0                                  # настройка меняется на лету
    assert a.current() == "idle"
    for raw, want in ((None, dom.DEFAULT_TTL_S), ("x", dom.DEFAULT_TTL_S), (True, dom.DEFAULT_TTL_S), (0, dom.TTL_MIN_S),
                      (999, dom.TTL_MAX_S), (4, 4.0)):
        assert dom.clamp_ttl(raw) == want


def test_context_fact_names_the_emotion_only_while_it_lasts():
    c = Clock()
    a = svc(c)
    cb = ContextBuilder(object())
    cb.emotion_of = a.emotion_block
    assert cb.emotion_of() == ""
    a.set("surprised", "chat")
    fact = cb.emotion_of()
    assert "«surprised»" in fact and "причина: chat" in fact and "не меняет факты" in fact
    c.t += 10
    assert cb.emotion_of() == ""


def test_fast_brain_publish_sets_the_emotion():
    a = svc()
    fast_brain.set_affect(a)
    try:
        fast_brain.publish("chat", {"emotion": "angry", "action": "none"})
        assert a.current() == "angry"
        fast_brain.publish("say", {"emotion": "happy", "action": "none"})
        assert a.current() == "happy"
    finally:
        fast_brain.set_affect(None)


async def test_presence_gets_idle_when_the_emotion_expires():
    got = []
    a = AffectService(lambda: 0.5, on_change=got.append)
    a.set("scared", "event")
    a._timer.cancel()                               # не ждём настоящих секунд: вызываем срок сами
    a._expire(a.state.ts)
    assert got and got[-1]["emotion"] == "idle"
    a.set("happy", "event")                         # устаревший таймер новую эмоцию не гасит
    got.clear()
    a._expire(a.state.ts - 1)
    assert got == []
    a._timer.cancel()


# ── эмоцию выбирает классификатор ──────────────────────────────────────────────

def ans(value, conf):
    return {"answer": value, "confidence": conf}


def test_classifier_emotion_needs_confidence_and_is_never_idle():
    assert decide.parse_input({"emotion": ans("angry", 0.6)}, 0.5).emotion == "angry"
    assert decide.parse_input({"emotion": ans("angry", 0.2)}, 0.5).emotion is None
    assert decide.parse_input({"emotion": ans("idle", 0.9)}, 0.5).emotion is None
    assert decide.parse_input({"emotion": ans("furious", 0.9)}, 0.5).emotion is None
    assert decide.parse_event({"emotion": ans("threat", 0.4)}, 0.9).emotion == "threat"     # свой порог, не порог категории
    assert "emotion" in decide.EVENT_QUESTIONS and "emotion" in decide.INPUT_QUESTIONS
    assert set(decide.EVENT_QUESTIONS["emotion"]["options"]) == set(fast_brain.EMOTIONS)


async def test_user_message_emotion_from_classifier_goes_to_the_hook():
    seen = []

    class FakeDecider:
        last_ms = 1

        async def classify(self, text, questions, timeout=None):
            return {"intent": ans("chat", 0.9), "emotion": ans("happy", 0.7)}

    ctx = SimpleNamespace(
        att=lambda: {"classifier": True, "min_conf_input": 0.5}, decider=FakeDecider(),
        log=SimpleNamespace(log_nowait=lambda *a, **k: None), on_emotion=lambda label, reason: seen.append((label, reason)))
    cls = await EventClassifier(ctx, None).classify_input("ты молодец", lambda why: asyncio.sleep(0))
    assert cls.intent == "chat" and seen == [("happy", "chat")]


def test_voice_tone_limits_in_engine():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules" / "voice"))
    import tts_engine
    tts_engine.set_tone({"pitch_hz": 999, "rate_pct": -999})
    assert tts_engine._shifted("-25Hz", tts_engine._tone["pitch_hz"]) == "+15Hz"
    assert tts_engine._shifted("-12%", tts_engine._tone["rate_pct"]) == "-37%"
    tts_engine.set_tone(None)
    assert tts_engine._shifted("-25Hz", 0) == "-25Hz"
