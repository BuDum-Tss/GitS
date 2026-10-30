"""Шаги интерфейса -> команды мода: порядок, поля, отказ на мусор."""
import ui_steps


def test_building_a_bed_becomes_the_same_sequence_a_player_does():
    commands, error = ui_steps.plan([
        {"do": "open_tab", "tab": "Architect"}, {"do": "category", "id": "Furniture"},
        {"do": "designator", "id": "Bed", "stuff": "WoodLog", "rot": "North", "лишнее": 1}, {"do": "place", "x": 45, "z": 30}])
    assert error is None
    assert [c["type"] for c in commands] == ["open_tab", "ui_category", "ui_designator", "ui_place"]
    assert commands[2] == {"type": "ui_designator", "id": "Bed", "stuff": "WoodLog", "rot": "North"}      # лишние поля отброшены
    assert commands[3] == {"type": "ui_place", "x": 45, "z": 30}


def test_drag_assign_and_pass_through_commands():
    commands, error = ui_steps.plan([
        {"do": "drag", "x1": 1, "z1": 2, "x2": 9, "z2": 2, "shape": "line"},
        {"do": "assign", "pawn_id": "Human1", "kind": "food", "value": "Только растительное"},
        {"do": "draft", "pawn_id": "Human1"}, {"do": "move", "pawn_id": "Human1", "x": 5, "z": 6}])
    assert error is None
    assert commands[0] == {"type": "ui_drag", "x1": 1, "z1": 2, "x2": 9, "z2": 2, "shape": "line"}
    assert commands[1]["type"] == "ui_assign" and commands[1]["kind"] == "food"
    assert commands[2] == {"type": "draft", "pawn_id": "Human1"} and commands[3]["type"] == "move"      # как есть


def test_bad_input_is_refused_before_anything_runs():
    for bad in (None, [], "place", [{"x": 1}], [{"do": 5}], [{"do": "взлететь"}], [{"do": "cancel"}] * 13):
        commands, error = ui_steps.plan(bad)
        assert commands == [] and error, bad
    assert "неизвестное действие" in ui_steps.plan([{"do": "взлететь"}])[1]


def test_letters_and_quests_are_steps():
    commands, error = ui_steps.plan([
        {"do": "letter", "index": 0, "action": "choose", "choice": "Принять", "лишнее": 1},
        {"do": "quest", "id": 7, "action": "accept", "pawn_id": "Human1"}])
    assert error is None
    assert commands == [{"type": "ui_letter", "index": 0, "action": "choose", "choice": "Принять"},
                        {"type": "ui_quest", "id": 7, "action": "accept", "pawn_id": "Human1"}]
