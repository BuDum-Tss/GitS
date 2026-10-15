"""Воспоминания персонажа (ТЗ 05, CHR-15, CHR-19): проверка memories.yaml, отбор в контекст, напоминание, блок промпта."""
from pathlib import Path

import pytest

import character
import character_memory as cm

SAMPLE_MEMORIES = Path(__file__).resolve().parents[1] / "characters" / "glados" / "memories.yaml"


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "memories.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_sample_file_covers_every_type_and_loads():
    items = cm.load_memories(SAMPLE_MEMORIES)
    assert len(items) >= 8, "образец из 8-10 записей (CHR-21: полное наполнение пишет владелец)"
    assert {i["type"] for i in items} == set(cm.TYPES)
    assert all(0.0 <= i["weight"] <= 1.0 for i in items)


def test_missing_file_is_an_empty_list(tmp_path):
    assert cm.load_memories(tmp_path / "нет.yaml") == []


@pytest.mark.parametrize("body, message", [
    ("- {id: a, type: biography, text: один}\n- {id: a, type: biography, text: два}\n", "повторяется"),
    ("- {id: a, type: dream, text: сон}\n", "type «dream»"),
    ("- {id: a, type: self, text: ''}\n", "text пустой"),
    ("- {id: a, type: self, text: " + "я " * 300 + "}\n", "text пустой или длиннее"),
    ("- {id: a, type: self, text: ок, weight: 1.5}\n", "вне диапазона"),
    ("- {id: a, type: self, text: ок, about: []}\n", "about не может быть пустым"),
    ("- {type: self, text: ок}\n", "нужен id"),
])
def test_bad_records_are_rejected_with_the_record_number(tmp_path, body, message):
    with pytest.raises(character.CharacterError, match=message):
        cm.load_memories(_write(tmp_path, body))


def _cand(mem_id, fact, mtype="self", weight=0.5):
    return {"mem_id": mem_id, "fact": fact, "mtype": mtype, "weight": weight, "source": "seed"}


def test_pick_keeps_the_best_three_above_threshold_by_relevance_times_weight():
    cands = [_cand("a", "про кофе", weight=1.0), _cand("b", "про лабораторию", weight=0.2),
             _cand("c", "про людей", weight=0.9), _cand("d", "про погоду", weight=0.9), _cand("e", "про тесты", weight=0.5)]
    scores = [0.6, 0.9, 0.7, 0.3, 0.8]               # d ниже порога 0.5
    picked, nudged = cm.pick(cands, scores, min_score=0.5)
    # релевантность × (0.5 + вес): c 0.7×1.4=0.98, a 0.6×1.5=0.90, b 0.9×0.7=0.63, e 0.8×1.0=0.80; d не прошла порог
    assert [c["mem_id"] for c in picked] == ["c", "a", "e"]
    assert not nudged
    assert all(c["score"] >= 0.5 for c in picked)


def test_nothing_above_threshold_gives_nothing_without_nudge():
    picked, nudged = cm.pick([_cand("a", "x")], [0.1], min_score=0.5)
    assert picked == [] and not nudged


def test_nudge_brings_the_heaviest_habit_or_opinion_when_nothing_fits():
    cands = [_cand("h1", "привычка любить паузы", mtype="habit", weight=0.4),
             _cand("o1", "мнение о людях", mtype="opinion", weight=0.8),
             _cand("g1", "шутка", mtype="gag", weight=1.0)]
    picked, nudged = cm.pick(cands, [0.0, 0.0, 0.0], min_score=0.5, nudge=True)
    assert nudged and [c["mem_id"] for c in picked] == ["o1"]
    assert picked[0]["nudge"] is True


def test_nudge_is_not_a_gag_or_a_biography():
    cands = [_cand("g1", "шутка", mtype="gag", weight=1.0), _cand("b1", "детство", mtype="biography", weight=1.0)]
    assert cm.pick(cands, [0.0, 0.0], min_score=0.5, nudge=True) == ([], False)


def test_block_has_the_heading_and_one_line_per_memory():
    assert cm.block([]) == ""
    text = cm.block([{"fact": "первое"}, {"fact": "второе"}])
    assert text.splitlines()[0] == "Твои воспоминания (это о тебе):"
    assert text.splitlines()[1:] == ["- первое", "- второе"]
