"""Журнал оценок: запись, итог рефлекса, отметка игрока, точность по отметкам."""
import appraisal_log as al


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(al, "_DIR", tmp_path)


def test_append_outcome_label_and_accuracy(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    answers = {"kind": {"answer": "combat", "confidence": 0.72}, "danger": {"answer": "yes", "confidence": 0.6, "noul": 0.6}}
    first = al.append("incident", "налёт 5 бандитов", answers, {"importance": 3, "emotion": "threat", "action": "draft_colonists"})
    second = al.append("letter", "торговец предлагает сталь", {"kind": {"answer": "work", "confidence": 0.4}}, {"importance": 0})
    al.set_outcome(first, "призвала 3 колонистов")
    rows = al.recent(10)
    assert [r["id"] for r in rows] == [second, first]                         # новые первыми
    assert rows[1]["outcome"] == "призвала 3 колонистов" and rows[1]["model"]["kind"]["answer"] == "combat"
    assert al.label(first, True) and al.label(second, False, correct_kind="trade")
    assert not al.label("нет-такого", True)
    st = al.accuracy()
    assert st["labeled"] == 2 and st["ok"] == 1 and st["accuracy"] == 0.5 and st["kind_mistakes"] == [("work", "trade")]
    assert al.recent(10)[0]["label"]["correct_kind"] == "trade"


def test_ids_are_unique_even_within_one_millisecond(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    monkeypatch.setattr(al.time, "time", lambda: 1000.0)               # время стоит: все записи в одну миллисекунду
    ids = [al.append("letter", f"письмо {i}", {}, {}) for i in range(5)]
    assert len(set(ids)) == 5
    assert [r["id"] for r in al.recent(10)] == ids[::-1]
