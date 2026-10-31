"""Домен: правила по тексту (ТЗ 29, ARC-2): «замолчи» и «похоже на просьбу». Одна точка истины для attention и router."""
import pytest

from domain import rules


@pytest.mark.parametrize("text", ["замолчи", "Пожалуйста, стоп!", "стоп", "хватит болтать", "тихо", "shut up"])
def test_stop_phrases(text):
    assert rules.looks_like_stop(text)


@pytest.mark.parametrize("text", ["привет", "я помолчу", "стоп, давай расскажу про погоду завтра утром и вечером", ""])
def test_not_stop(text):
    assert not rules.looks_like_stop(text)


def test_stop_is_matched_on_whole_short_phrase_only():
    assert not rules.looks_like_stop("x" * 50 + " стоп")          # длиннее 40 символов: это не короткая просьба
