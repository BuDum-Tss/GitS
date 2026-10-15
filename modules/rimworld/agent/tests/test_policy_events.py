"""ТЗ 11e, POL-3: каталог событий, превращение событий игры в виды каталога, классификатор только для текстовых видов."""
import asyncio

import policies
import policy_events as pe


def test_mod_events_map_to_catalog_kinds_with_catalog_importance():
    ev = pe.from_mod({"type": "incident", "data": {"label": "рейд пиратов"}, "priority": "critical"})
    assert ev.kind == "raid" and ev.importance == 5 and ev.fields == {"label": "рейд пиратов"}
    assert pe.from_mod({"type": "pawn_died", "data": {"name": "Ада", "cause": "голод"}}).kind == "death"
    assert pe.from_mod({"type": "unknown_thing"}) is None


def test_statediff_lines_map_only_when_important():
    assert pe.from_statediff("исследование завершено: Орошение", True).fields == {"research": "Орошение"}
    assert pe.from_statediff("исследование завершено: Орошение", False) is None      # неважное не событие
    assert pe.from_statediff("Ада упал(а) и лежит", True).kind == "pawn_downed"
    assert pe.from_statediff("запас еды: 6 → 2 дн.", True).kind == "resource_out"
    assert pe.from_statediff("погода: дождь", True) is None


def test_zero_importance_is_dropped_before_any_classifier():
    assert pe.make("research_done", "x", importance=0) is None


def test_monitor_events_use_catalog_importance():
    ev = pe.make("task_stalled", "задача застряла: стена", {"description": "стена"})
    assert ev.kind == "task_stalled" and ev.importance == 3


def test_unknown_kind_is_not_an_event():
    assert pe.make("no_such_kind", "x") is None


def test_classifier_changes_kind_of_text_event_only_when_confident():
    async def sure(text, questions):
        assert "illness" in questions["kind"]["options"]
        return {"kind": {"answer": "illness", "confidence": 0.8}}

    async def unsure(text, questions):
        return {"kind": {"answer": "illness", "confidence": 0.3}}

    async def down(text, questions):
        raise ConnectionError("нет сервиса")

    letter = pe.make("letter", "игровое письмо: болезнь у Ады", {"label": "x"})
    out = asyncio.run(pe.classify(letter, decide=sure))
    assert out.kind == "illness" and out.classified and out.fields.get("was") == "letter"
    assert asyncio.run(pe.classify(letter, decide=unsure)).kind == "letter"
    assert asyncio.run(pe.classify(letter, decide=down)).kind == "letter"
    assert asyncio.run(pe.classify(letter, decide=None)).kind == "letter"


def test_structured_events_skip_the_classifier():
    called = []

    async def spy(text, questions):
        called.append(text)
        return None

    raid = pe.make("raid", "угроза", {"label": "x"})
    assert asyncio.run(pe.classify(raid, decide=spy)) is raid and called == []


def test_questions_offer_every_catalog_kind():
    q = pe.questions(policies.get())
    assert set(q["kind"]["options"]) == set(policies.get().kinds)
