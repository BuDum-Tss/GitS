"""Домен: оценка внимания (ТЗ 29, ARC-2). Чистые функции, значения посчитаны по прежней формуле `Attention._score` и `_decide`."""
import pytest

from domain import scoring

THRESHOLDS = (15, 35, 60, 85)


def test_priority_is_urgency_times_20_times_weights():
    assert scoring.priority(3, 1, 1, 1, 1, 1.0) == 60.0
    assert scoring.priority(5, 1.2, 1.0, 1.0, 0.3, 1.0) == pytest.approx(36.0)


def test_priority_is_clamped_to_0_100():
    assert scoring.priority(5, 1.5, 1.5, 1, 1, 1) == 100.0
    assert scoring.priority(0, 1, 1, 1, 1, 1) == 0.0


def test_freshness_halves_per_half_life():
    assert scoring.freshness(0, 300) == 1.0
    assert scoring.freshness(300, 300) == 0.5
    assert scoring.freshness(600, 300) == 0.25


def test_freshness_of_negative_age_is_one():
    assert scoring.freshness(-5, 300) == 1.0


@pytest.mark.parametrize("priority, expected", [(10, "D0"), (20, "D1"), (40, "D2"), (60, "D3"), (70, "D3"), (90, "D4")])
def test_decide_by_thresholds_with_idle_focus(priority, expected):
    assert scoring.decide(priority, THRESHOLDS, False, "idle", 0, 15) == expected


def test_decide_escalates_to_d4_over_busy_focus_by_margin():
    # поверх разговора (приоритет 60): нужно выше 60 + 15 = 75
    assert scoring.decide(70, THRESHOLDS, False, "conversation", 60, 15) == "D3"
    assert scoring.decide(80, THRESHOLDS, False, "conversation", 60, 15) == "D4"


def test_decide_away_user_sends_important_to_digest():
    assert scoring.decide(90, THRESHOLDS, True, "idle", 0, 15) == "D1"
    assert scoring.decide(65, THRESHOLDS, True, "idle", 0, 15) == "D1"
    assert scoring.decide(10, THRESHOLDS, True, "idle", 0, 15) == "D0"


def test_half_life_uses_longest_matching_pattern():
    table = {"rimworld.*": 60, "rimworld.col*": 30, "*": 300}
    assert scoring.half_life("rimworld.colonists", table, 300) == 30.0
    assert scoring.half_life("telegram.msg", {"telegram.*": 900, "*": 300}, 300) == 900.0
    assert scoring.half_life("x.y", {"telegram.*": 900}, 300) == 300.0


def test_half_life_skips_non_positive_and_non_numeric_values():
    assert scoring.half_life("telegram.msg", {"telegram.*": 0, "*": 300}, 300) == 300.0
    assert scoring.half_life("telegram.msg", {"telegram.*": "900"}, 300) == 300.0
    assert scoring.half_life("telegram.msg", {"telegram.*": -5}, 120) == 120.0
