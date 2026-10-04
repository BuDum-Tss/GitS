"""Распознавание просьб по глаголам: формы «запусти», «запускай» и т.п. должны уходить в планировщик."""
import pytest

from router import Router


@pytest.mark.parametrize("text", ["запусти римворлд", "Круто! тогда запускай римворлд. пойдем играть", "стартуй игру",
                                  "давай поиграем в римворлд", "установи и настрой telethon", "включи голос"])
def test_requests_are_recognised(text):
    assert Router.looks_like_action(text)


@pytest.mark.parametrize("text", ["привет, как дела", "сегодня смотрела сериал", "мне грустно"])
def test_small_talk_is_not_a_request(text):
    assert not Router.looks_like_action(text)
