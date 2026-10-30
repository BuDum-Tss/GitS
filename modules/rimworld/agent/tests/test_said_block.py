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


def test_turn_fact_skips_read_only_calls():
    """Ход из одних чтений и меню не даёт фактов: ей не о чем рассказывать (раньше она просила показать экран)."""
    import controller
    c = controller.Controller.__new__(controller.Controller)
    read_only = {"contour": "turn", "trace": [{"name": "get_colony_state"}, {"name": "switch_mode"}, {"name": "open_game_tab"}], "notes": []}
    assert c._turn_fact(read_only) == ""
    acted = {"contour": "turn", "trace": [{"name": "get_colony_state"}, {"name": "set_work_priorities", "result": "ok"}], "notes": []}
    fact = c._turn_fact(acted)
    assert "set_work_priorities" in fact and "get_colony_state" not in fact
    assert "не проси скриншот" in fact.lower()
