"""Игровой ход Claude видит, что она сказала вслух (ходы озвучивает её голос), и отличает это от того, что реально делает."""
import controller


class Link:
    def __init__(self, said):
        self._said = said

    def recent_said(self, n=5):
        return self._said[-n:]


def make(link):
    c = controller.Controller.__new__(controller.Controller)
    c.link = link
    return c


def test_said_block_lists_her_recent_lines_and_tells_claude_to_keep_promises():
    text = make(Link(["Начинаем с укреплений.", "Стены растут."]))._said_block()
    assert "ЧТО ТЫ СКАЗАЛА ВСЛУХ" in text and "- Начинаем с укреплений." in text and "- Стены растут." in text
    assert "обещанное ходом выполни" in text


def test_said_block_is_empty_without_lines_or_link():
    assert make(Link([]))._said_block() == ""
    assert make(None)._said_block() == ""
