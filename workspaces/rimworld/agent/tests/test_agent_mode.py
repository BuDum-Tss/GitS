"""Режим игрового агента: переходы, история, фраза для подсказки."""
import agent_mode as am


def test_modes_history_and_line():
    am.set_mode("observe", "")
    am.set_mode("plan", "чертежи висят")
    assert am.get()["mode"] == "plan" and "планирует: чертежи висят" in am.line()
    am.set_mode("observe", "")
    recent = am.get()["recent"]
    assert recent[0]["mode"] == "plan" and recent[0]["reason"] == "чертежи висят"


def test_unknown_mode_is_ignored():
    am.set_mode("observe", "")
    am.set_mode("dance", "x")
    assert am.get()["mode"] == "observe"


def test_graph_shows_game_modes_with_their_tools():
    import game_modes
    am.set_game_mode("build")
    g = am.graph()
    assert [n["id"] for n in g["nodes"] if n.get("current")] == ["build"]
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {"agent", *game_modes.MODES} and all(e["from"] in ids and e["to"] in ids for e in g["edges"])
    node = {n["id"]: n for n in g["nodes"]}
    assert node["build"]["tools"] == list(game_modes.MODES["build"].tools) and node["agent"]["tools"] == list(game_modes.CORE)
    assert sum(1 for n in g["nodes"] if n.get("root")) == 1
    am.set_game_mode("observe")
