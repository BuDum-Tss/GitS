"""Переключатель камеры: агент или игрок. Файл состояния, отказ инструментам и командам контроллера."""
import camera_gate
import controller
import rimworld_mcp


def use_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("RIMWORLD_MEMORY_DIR", str(tmp_path))
    camera_gate._cache.update(at=0.0, value=True)


def test_agent_has_the_camera_by_default_and_after_a_broken_file(monkeypatch, tmp_path):
    use_memory(monkeypatch, tmp_path)
    assert camera_gate.agent_has_camera() and not camera_gate.blocks("camera_goto")
    (tmp_path / "camera_agent.json").write_text("{не json", encoding="utf-8")
    camera_gate._cache.update(at=0.0, value=True)
    assert camera_gate.agent_has_camera()


def test_switch_off_blocks_moving_commands_but_not_reading_the_view(monkeypatch, tmp_path):
    use_memory(monkeypatch, tmp_path)
    camera_gate.set_agent_camera(False)
    assert not camera_gate.agent_has_camera()
    for t in ("camera_goto", "camera_jump", "camera_follow", "camera_zoom", "camera_move", "camera_track", "set_auto_camera"):
        assert camera_gate.blocks(t), t
    assert not camera_gate.blocks("camera_view") and not camera_gate.blocks("build")
    camera_gate.set_agent_camera(True)
    assert not camera_gate.blocks("camera_goto")


def test_mcp_tools_and_controller_do_not_touch_the_game_when_the_camera_is_the_players(monkeypatch, tmp_path):
    use_memory(monkeypatch, tmp_path)
    camera_gate.set_agent_camera(False)

    def boom(*a, **k):
        raise AssertionError("команда камеры дошла до игры")
    monkeypatch.setattr(rimworld_mcp.httpx, "post", boom)
    monkeypatch.setattr(controller.httpx, "post", boom)
    assert rimworld_mcp.camera_look(10, 10) == camera_gate.REFUSAL
    assert rimworld_mcp.camera_move(1, 1) == camera_gate.REFUSAL
    assert controller._camera_command({"type": "camera_goto", "x": 1, "z": 1}) is None
