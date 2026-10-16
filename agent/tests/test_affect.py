"""ТЗ 26: эмоции персонажа. Часы управляемые, модель и сеть не нужны."""
import json
import sys
from pathlib import Path

import fast_brain
import journal
from app.affect_service import AffectService
from app.context_builder import ContextBuilder
from domain import affect as dom

P = dom.Params.from_card({"baseline_valence": 0.1, "emotion_decay_min": 10, "mood_decay_h": 2, "inertia": 0.0,
                          "max_shift_per_hour": 0.5})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def svc(tmp_path, clock=None, params=P):
    return AffectService(tmp_path, params, clock=clock or Clock())


def test_pet_labels_have_vectors():
    assert set(dom.VECTORS) == set(fast_brain.EMOTIONS)


def test_empty_card_is_neutral():
    p = dom.Params.from_card({})
    assert (p.baseline_valence, p.inertia) == (0.0, 0.3)
    assert dom.Params.from_card({"inertia": "x", "baseline_valence": 9}).baseline_valence == 1.0


def test_decay_returns_to_baseline_and_is_stepwise_exact():
    s, _ = dom.appraise(dom.initial(P, 0), P, 0, label="angry", intensity=3, category="game")
    assert s.valence < 0.1
    one = dom.decay(s, P, 7200)
    step = s
    for t in range(600, 7201, 600):
        step = dom.decay(step, P, t)
    assert abs(one.valence - step.valence) < 1e-9 and abs(one.mood - step.mood) < 1e-9
    far = dom.decay(s, P, 10 * 86400)
    assert abs(far.valence - 0.1) < 1e-6 and abs(far.mood - 0.1) < 1e-6 and not far.emotions


def test_saturation_and_hour_cap():
    p = dom.Params.from_card({"inertia": 0, "max_shift_per_hour": 2, "sensitivity": {"game": 5}})
    s = dom.initial(p, 0)
    prev = 0
    for _ in range(30):
        s, _w = dom.appraise(s, p, 0, label="happy", intensity=3, category="game")
        assert prev <= s.valence <= 1.0
        prev = s.valence
    assert s.valence > 0.9
    capped = dom.initial(P, 0)
    warns = []
    for _ in range(10):
        capped, w = dom.appraise(capped, P, 0, label="sad", intensity=3, category="game")
        warns += w
    assert warns and abs(capped.valence - 0.1) <= P.max_shift_per_hour + 1e-9


def test_inertia_reduces_shift():
    p0 = dom.Params.from_card({"inertia": 0.0})
    p9 = dom.Params.from_card({"inertia": 0.8})
    a, _ = dom.appraise(dom.initial(p0, 0), p0, 0, label="sad", intensity=3, category="game")
    b, _ = dom.appraise(dom.initial(p9, 0), p9, 0, label="sad", intensity=3, category="game")
    assert abs(b.valence) < abs(a.valence)


def test_neutral_labels_do_not_move_state():
    s0 = dom.initial(P, 0)
    s, _ = dom.appraise(s0, P, 0, label="thinking", intensity=3, category="chat")
    assert s.valence == s0.valence and not s.emotions


def test_bounds_for_tone_and_proactivity():
    s = dom.State(valence=-1, arousal=1, mood=-1, ts=0)
    t = dom.voice_tone(s, P)
    assert abs(t["rate_pct"]) <= P.voice_rate_limit and abs(t["pitch_hz"]) <= P.voice_pitch_limit
    assert abs(dom.proactivity_offset(s, P)) <= P.proactivity_limit
    assert dom.proactivity_offset(s, P) > 0 > dom.proactivity_offset(dom.State(mood=1, ts=0), P)


def test_bad_events_drop_mood_then_recover(tmp_path):
    c = Clock()
    a = svc(tmp_path, c)
    base = a.snapshot()["mood"]
    for _ in range(3):
        a.on_emotion("pawn_died", {"emotion": "sad", "intensity": 3})
    low = a.snapshot()
    assert low["mood"] < base and low["mood_emotion"] == "sad" and "pawn_died" in low["reason"]
    assert "не меняет факты" in a.mood_block()
    c.t += 20 * 3600
    assert abs(a.snapshot()["mood"] - P.baseline_valence) < 0.01


def test_own_reply_does_not_move_state_and_intensity_from_state(tmp_path):
    a = svc(tmp_path)
    before = a.snapshot()["valence"]
    a.on_emotion("say", {"emotion": "angry", "intensity": 3})
    assert a.snapshot()["valence"] == before
    assert a.reply_intensity("angry") == 0
    a.on_emotion("chat", {"emotion": "angry", "intensity": 3})
    assert a.reply_intensity("angry") >= 1


def test_relation_delta_and_mistake(tmp_path):
    a = svc(tmp_path, params=dom.Params.from_card({"baseline_valence": 0.1, "inertia": 0.0, "max_shift_per_hour": 2}))
    a.on_emotion("chat", {"emotion": "looking", "intensity": 1, "delta": -10})
    v = a.snapshot()["valence"]
    assert v < 0.1
    a.on_mistake("denied")
    assert a.snapshot()["valence"] < v


def test_persist_restore_and_sleep_decay(tmp_path):
    c = Clock()
    a = svc(tmp_path, c)
    a.on_emotion("chat", {"emotion": "angry", "intensity": 3})
    mood = a.snapshot()["mood"]
    a.stop()
    assert json.loads((tmp_path / "affect.json").read_text(encoding="utf-8"))["state"]["mood"] == mood
    c.t += 3600
    b = svc(tmp_path, c)
    assert P.baseline_valence > b.snapshot()["mood"] > mood
    (tmp_path / "affect.json").write_text("{мусор", encoding="utf-8")
    assert abs(svc(tmp_path, c).snapshot()["mood"] - P.baseline_valence) < 1e-9


def test_determinism(tmp_path):
    def run(d):
        d.mkdir()
        c = Clock()
        a = svc(d, c)
        for i in range(5):
            c.t += 60
            a.on_emotion("chat", {"emotion": "happy" if i % 2 else "angry", "intensity": 2})
        return a.snapshot(with_history=True)
    assert run(tmp_path / "1") == run(tmp_path / "2")


def test_character_switch_keeps_own_states(tmp_path):
    c = Clock()
    one, two = tmp_path / "one", tmp_path / "two"
    a = AffectService(one, P, clock=c)
    a.on_emotion("chat", {"emotion": "sad", "intensity": 3})
    mood_one = a.snapshot()["mood"]
    a.switch(two, dom.Params.from_card({"baseline_valence": 0.5}))
    assert a.snapshot()["mood"] == 0.5
    a.on_emotion("chat", {"emotion": "happy", "intensity": 3})
    a.switch(one, P)
    assert abs(a.snapshot()["mood"] - mood_one) < 1e-9
    assert (two / "affect.json").exists()


def test_reset(tmp_path):
    a = svc(tmp_path)
    a.on_emotion("chat", {"emotion": "angry", "intensity": 3})
    a.reset()
    assert abs(a.snapshot()["mood"] - P.baseline_valence) < 1e-9 and not a.snapshot()["emotions"]


def test_context_and_fast_brain_wiring(tmp_path):
    a = svc(tmp_path)
    cb = ContextBuilder(object())
    cb.mood_of = a.mood_block
    assert "не меняет факты" in cb.mood_of()
    fast_brain.set_affect(a)
    try:
        fast_brain.publish("chat", {"emotion": "angry", "intensity": 3, "delta": -5})
        assert a.snapshot()["emotions"].get("angry")
        fast_brain.publish("say", {"emotion": "angry"})
    finally:
        fast_brain.set_affect(None)
    assert journal.mistake_hook is None or callable(journal.mistake_hook)


def test_voice_tone_limits_in_engine():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "modules" / "voice"))
    import tts_engine
    tts_engine.set_tone({"pitch_hz": 999, "rate_pct": -999})
    assert tts_engine._shifted("-25Hz", tts_engine._tone["pitch_hz"]) == "+15Hz"
    assert tts_engine._shifted("-12%", tts_engine._tone["rate_pct"]) == "-37%"
    tts_engine.set_tone(None)
    assert tts_engine._shifted("-25Hz", 0) == "-25Hz"
