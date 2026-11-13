"""Поиск инструментов по словам: запрос, не совпавший ни с чем (например, на другом языке), отдаёт инструменты модуля фокуса,
а не первые по порядку. Живая репетиция: агент в среде игры получал инструменты ассистента и говорил, что игровых нет."""
from environments.base import Environment, Tool
from environments.registry import Registry


def _registry(focus):
    reg = Registry()
    core = Environment("assistant", "Ассистент", "напоминания и память")
    core.tools = [Tool(f"assistant.t{i}", "assistant", f"t{i}", "напомнить о деле позже") for i in range(20)]
    game = Environment("game", "Игра", "колония", module="game")
    game.tools = [Tool(f"game.g{i}", "game", f"g{i}", "построить зону склада в колонии") for i in range(6)]
    reg.envs = {"assistant": core, "game": game}
    reg.focus = (lambda: focus) if focus is not None else None
    return reg


def test_unmatched_query_returns_focused_module_tools_first():
    found = _registry({"game"}).search("inspect colony resources and build stockpile")
    assert [t.env for t in found[:6]] == ["game"] * 6


def test_matching_words_still_win_over_focus():
    found = _registry({"game"}).search("напомнить о деле")
    assert found[0].env == "assistant"


def test_without_focus_order_is_unchanged():
    found = _registry(None).search("inspect colony resources")
    assert [t.id for t in found[:3]] == ["assistant.t0", "assistant.t1", "assistant.t2"]
