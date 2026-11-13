"""Домен: политика риска инструмента (ТЗ 29, ARC-2): когда нужно разрешение человека. Логика прежней `Router._step`."""
import pytest

from domain import risk


@pytest.mark.parametrize("danger, rk, self_confirming, telegram_confirm, expected", [
    (None, "auto", False, False, False),
    (None, "auto", False, True, False),                 # настройка подтверждения не касается обычных действий
    ("удаляет файлы", "auto", False, False, True),      # опасная часть всегда спрашивает
    (None, "ask", True, False, True),                   # инструмент сам просит разрешение
    (None, "public", False, True, True),                # публикация без самоподтверждения при включённой настройке
    (None, "public", True, True, False),                # инструмент подтверждает себя сам
    (None, "public", False, False, False),              # настройка выключена
])
def test_needs_ask(danger, rk, self_confirming, telegram_confirm, expected):
    assert risk.needs_ask(danger, rk, self_confirming, telegram_confirm) is expected
