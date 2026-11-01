"""Тетрадь экспериментов для роликов: один активный, записи, моменты становятся метками, закадровый текст вместо живой речи."""
import pytest

import experiments


def test_experiment_lifecycle(tmp_path):
    d = str(tmp_path)
    assert experiments.summary(d).startswith("активного эксперимента нет")
    exp = experiments.start("Колония без еды", "Продержатся ли 5 суток на дичи", now=1.0, memory_dir=d)
    assert exp["id"] == 1
    experiments.log("Сломали холодильник", kind="moment", importance=3, now=2.0, memory_dir=d)
    experiments.log("Ставлю ловушки у реки", now=3.0, memory_dir=d)
    experiments.finish("Продержались 3 суток", "Нужны ловушки раньше", now=4.0, memory_dir=d)
    text = experiments.summary(d)
    assert "Продержались 3 суток" in text and "Нужны ловушки раньше" in text
    with pytest.raises(ValueError):
        experiments.log("поздно", memory_dir=d)                       # активного нет


def test_new_experiment_aborts_the_previous_one(tmp_path):
    d = str(tmp_path)
    experiments.start("Первый", "гипотеза 1", now=1.0, memory_dir=d)
    experiments.start("Второй", "гипотеза 2", now=2.0, memory_dir=d)
    data = experiments.load(d)
    assert [e["status"] for e in data["experiments"]] == ["aborted", "active"]
    with pytest.raises(ValueError):
        experiments.start("", "без названия", memory_dir=d)


def test_moments_and_narration_become_marks_once(tmp_path):
    d = str(tmp_path)
    experiments.start("Э", "г", now=1.0, memory_dir=d)
    experiments.log("шаг", kind="step", now=2.0, memory_dir=d)
    experiments.log("повар сошёл с ума", kind="moment", importance=3, now=3.0, memory_dir=d)
    experiments.log("Какая неожиданность.", kind="narration", now=4.0, memory_dir=d)
    pending = experiments.unmarked_moments(d)
    assert [e["kind"] for e in pending] == ["moment", "narration"]       # шаги метками не бывают
    experiments.mark_done(pending, d)
    assert experiments.unmarked_moments(d) == []
    with pytest.raises(ValueError):
        experiments.log("x", kind="что-то", memory_dir=d)


def test_entries_are_capped(tmp_path):
    d = str(tmp_path)
    experiments.start("Э", "г", now=0.0, memory_dir=d)
    for i in range(experiments.MAX_ENTRIES + 5):
        experiments.log(f"шаг {i}", now=float(i), memory_dir=d)
    assert len(experiments.active(experiments.load(d))["entries"]) == experiments.MAX_ENTRIES


def test_abandon_active_closes_the_running_experiment(tmp_path):
    d = str(tmp_path)
    assert experiments.abandon_active("новый забег", memory_dir=d) is None
    experiments.start("Стены", "защищают", memory_dir=d)
    closed = experiments.abandon_active("новый забег", memory_dir=d)
    assert closed["status"] == "aborted" and closed["conclusion"] == "новый забег"
    assert experiments.active(experiments.load(d)) is None
