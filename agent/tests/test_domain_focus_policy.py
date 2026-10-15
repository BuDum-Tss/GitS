"""Домен: политика фокуса (ТЗ 29, ARC-2, ТЗ 18). Чистые функции без часов и состояния; значения по прежним правилам FocusManager."""
from domain import focus_policy as fp


def test_aging_raises_priority_per_step_and_caps_it():
    assert fp.aged_priority(20, 0) == 20
    assert fp.aged_priority(20, 299.9) == 20                 # меньше одного шага AGING_S
    assert fp.aged_priority(20, 300) == 30
    assert fp.aged_priority(20, 3000) == 70                  # не выше AGING_CAP
    assert fp.aged_priority(60, 3000) == 70


def test_clears_focus_needs_threshold_and_margin_over_holder():
    assert fp.clears_focus(60, 0)                            # порог переключения 60, фокуса нет
    assert not fp.clears_focus(59.9, 0)
    assert not fp.clears_focus(70, 60)                       # выше фокуса 60, но меньше чем на запас 15
    assert fp.clears_focus(75, 60)


def test_dampening_short_focus_time():
    assert fp.dampening(100, 90, []) == "short"              # в фокусе 10 с, минимум 20


def test_dampening_switch_rate_per_minute():
    assert fp.dampening(100, 50, [99, 98, 97, 96]) == "rate"  # четыре переключения за минуту
    assert fp.dampening(100, 50, [90, 80, 70]) is None        # три: ещё можно
    assert fp.dampening(100, 50, [30, 20, 10, 5]) is None     # старые переключения не считаются


def test_near_threshold_or_holder():
    assert fp.near_threshold(60, 0, False)                    # рядом с порогом переключения
    assert not fp.near_threshold(40, 0, False)
    assert fp.near_threshold(40, 30, True)                    # фокус занят, близко к его приоритету
    assert not fp.near_threshold(40, 30, False)               # без фокуса приоритет фокуса не важен


def test_hint_conflicts():
    assert not fp.hint_conflicts(None, 0.9, 70, True, "telegram")
    assert fp.hint_conflicts("rimworld/build", 0.7, 40, None, "telegram")     # другой модуль
    assert not fp.hint_conflicts("telegram/x", 0.7, 40, None, "telegram")
    assert fp.hint_conflicts("none", 0.7, 70, True, "telegram")               # «фокус не нужен», а важность высокая
    assert not fp.hint_conflicts("none", 0.7, 40, True, "telegram")
    assert not fp.hint_conflicts("rimworld/build", 0.5, 40, None, "telegram")  # уверенность ниже CONF_LOW


def test_claimant_is_pending_event_of_other_module_with_close_priority():
    others = [("e2", "b", "pending", False, 68)]
    assert fp.has_claimant(70, "a", "e1", others, "pending")
    assert not fp.has_claimant(70, "b", "e1", others, "pending")                     # тот же модуль
    assert not fp.has_claimant(70, "a", "e1", [("e2", "b", "deferred", False, 68)], "pending")
    assert not fp.has_claimant(70, "a", "e1", [("e2", "b", "pending", True, 68)], "pending")   # критическое
    assert not fp.has_claimant(40, "a", "e1", others, "pending")                     # ниже порога


def test_echelon_order():
    assert fp.echelon(True, False, False) == fp.ECHELON_GEMMA
    assert fp.echelon(False, True, True) == fp.ECHELON_RULES
    assert fp.echelon(False, False, True) == fp.ECHELON_GLINER
    assert fp.echelon(False, False, False) == fp.ECHELON_RULES


def test_confirmed_checks_ok_and_policy():
    assert fp.confirmed({"ok": True, "policy": "x"}, "x")
    assert fp.confirmed({"ok": True}, "x")                   # политику не назвал
    assert not fp.confirmed({"ok": True, "policy": "y"}, "x")
    assert not fp.confirmed({"ok": False}, None)
    assert not fp.confirmed("ok", None)


def test_hint_value_drops_junk():
    assert fp.hint_value("a/b") == "a/b"
    assert fp.hint_value("") is None
    assert fp.hint_value("x" * 121) is None
    assert fp.hint_value(5) is None
