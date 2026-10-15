"""Динамическая оценка событий: модель классифицирует, код выводит важность, повтор, динамику, рефлекс и длину реплики."""
import asyncio

import appraisal as ap
import decider


def _answers(kind="combat", danger=0.9, urgency="emergency", tone="bad"):
    return {"kind": {"type": "choice", "answer": kind, "confidence": 0.8},
            "danger": {"type": "noul", "answer": "yes" if danger >= 0.5 else "no", "noul": danger},
            "urgency": {"type": "choice", "answer": urgency, "confidence": 0.7},
            "tone": {"type": "choice", "answer": tone, "confidence": 0.7}}


def _fresh(monkeypatch):
    monkeypatch.setattr(ap, "AFFECT", ap.Affect())


def test_combat_emergency_drafts_and_goes_full():
    a = ap.derive(_answers(), "incident", "налёт 5 бандитов", ap.Affect(), now=1000.0)
    assert a.importance == 4 and a.action == "draft_colonists" and a.comment == "full" and a.emotion == "threat"
    assert a.claude == "urgent" and a.valence == "negative"


def test_routine_construction_is_quiet_after_a_repeat():
    aff = ap.Affect()
    first = ap.derive(_answers("work", 0.05, "routine", "good"), "letter", "стены достроены, запас дерева вырос", aff, now=1000.0)
    assert first.importance == 0 and first.action == "none" and first.emotion == "proud" and first.comment == "brief"
    aff.update(first, "стены достроены, запас дерева вырос", now=1000.0)
    again = ap.derive(_answers("work", 0.05, "routine", "good"), "letter", "стены достроены, запас дерева вырос", aff, now=1010.0)
    assert again.novelty == "repeated" and again.comment == "silent"          # повтор рутины: промолчать


def test_death_is_never_routine_and_streak_escalates():
    a = ap.derive(_answers("death", 0.1, "routine", "bad"), "pawn_died", "погиб Sean", ap.Affect(), now=1000.0)
    assert a.importance >= 3 and a.comment in ("brief", "full") and a.emotion == "sad"
    aff = ap.Affect()
    for i in range(3):
        aff.update(ap.derive(_answers(), "incident", f"налёт {i}", aff, now=1000.0 + i), f"налёт {i}", now=1000.0 + i)
    streak = ap.derive(_answers(), "incident", "налёт 4", aff, now=1004.0)
    assert streak.novelty == "repeated" and streak.trend == "escalating" and streak.emotion in ("scared", "angry")


def test_valence_comes_from_kind_and_routine_never_escalates():
    aff = ap.Affect()
    aff.arousal = 0.9
    aff._ts = 1000.0
    good = ap.derive(_answers("social", 0.0, "routine", "neutral"), "letter", "Rika и Silver друзья", aff, now=1000.0)
    assert good.valence == "positive" and good.trend != "escalating"          # высокое напряжение не делает рутину нарастанием
    assert ap.derive(_answers("fire", 0.8, "emergency", "neutral"), "fire_started", "пожар", aff, now=1000.0).valence == "negative"


def test_low_confidence_kind_is_ignored():
    a = _answers("combat", 0.2, "routine", "bad")
    a["kind"]["confidence"] = 0.2
    d = ap.derive(a, "x", "что-то непонятное", ap.Affect(), now=1000.0)
    assert d.action == "none" and d.importance == 0 and d.emotion == "looking"        # категория отброшена, рефлекса нет


def test_garbage_answers_fall_back_to_calm_defaults():
    a = ap.derive({"kind": {"answer": "nonsense"}, "danger": None}, "x", "что-то", ap.Affect(), now=1000.0)
    assert a.importance == 0 and a.action == "none" and a.emotion == "looking"


def test_affect_accumulates_and_decays():
    aff = ap.Affect()
    aff._ts = 1000.0
    a = ap.derive(_answers(), "incident", "налёт", aff, now=1000.0)
    aff.update(a, "налёт", now=1000.0)
    high = aff.arousal
    assert high > 0.6 and aff.valence < 0
    assert "налёт" in aff.context(now=1001.0) and aff.streak("incident", now=1001.0) == 1
    assert aff.mood_hint(now=1001.0).startswith("Общее настроение: напряжение")
    aff._decay(1000.0 + ap.HALF_LIFE)
    assert abs(aff.arousal - high / 2) < 0.02
    assert aff.mood_hint(now=1000.0 + 20 * ap.HALF_LIFE) == ""


def test_appraise_uses_decider_and_context(monkeypatch):
    _fresh(monkeypatch)
    seen = {}

    async def fake_decide(state, questions, timeout=None):
        seen["state"], seen["q"] = state, questions
        return _answers()
    monkeypatch.setattr(decider, "decide", fake_decide)
    first = asyncio.run(ap.appraise("incident", "налёт 5 бандитов"))
    assert first.action == "draft_colonists" and first.ms >= 0
    assert set(seen["q"]) == {"kind", "danger", "tone"}
    asyncio.run(ap.appraise("incident", "ещё налёт"))
    assert seen["state"] == "ещё налёт"                       # модель видит только событие, контекст учитывает код
    assert ap.AFFECT.streak("incident") == 2
    assert ap.recent() is not None and "важность 4/4" in ap.recent().tone()


def test_appraise_returns_none_without_service(monkeypatch):
    _fresh(monkeypatch)

    async def down(state, questions, timeout=None):
        return None
    monkeypatch.setattr(decider, "decide", down)
    assert asyncio.run(ap.appraise("incident", "x")) is None


def test_decider_client_marks_service_down(monkeypatch):
    monkeypatch.setattr(decider, "_down_until", [0.0])
    monkeypatch.setattr(decider, "_client", None)
    monkeypatch.setattr(decider, "URL", "http://127.0.0.1:9")
    assert asyncio.run(decider.decide("s", {"q": decider.choice(["a", "b"])}, timeout=0.5)) is None
    assert decider.available() is False
