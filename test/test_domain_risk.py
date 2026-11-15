"""Политика риска инструмента (ТЗ 29, ARC-2): когда нужно разрешение человека. Логика `Router._step`."""
import pytest

from services.tools import router as risk


@pytest.mark.parametrize("danger, rk, expected", [
    (None, "auto", False),
    ("удаляет файлы", "auto", True),         # опасная часть всегда спрашивает
    (None, "ask", True),                     # инструмент сам просит разрешение
    (None, "public", False),                 # публикация разрешения не требует: она публикует сама
    ("пишет в чат", "public", True),         # опасная часть публичного инструмента всё равно спрашивается
])
def test_needs_ask(danger, rk, expected):
    assert risk._needs_ask(danger, rk) is expected
