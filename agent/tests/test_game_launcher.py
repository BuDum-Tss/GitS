"""Запуск RimWorld без настоящей игры: подмена процессов и порта."""
import pytest

import game_launcher as gl


def test_already_running_does_not_start_second_copy(monkeypatch):
    monkeypatch.setattr(gl, "is_running", lambda: True)
    monkeypatch.setattr(gl, "mod_ready", lambda: True)
    started = []
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert "уже запущена" in gl.launch()
    assert started == []


def test_missing_exe_gives_clear_error(monkeypatch):
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: None)
    with pytest.raises(RuntimeError, match="GLADOS_RIMWORLD_EXE"):
        gl.launch()


def test_launch_waits_for_mod(monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    state = {"up": False, "ticks": 0}
    monkeypatch.setattr(gl, "is_running", lambda: bool(state.get("started")))
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: state.update(started=True))

    def ready():
        state["ticks"] += 1
        return state["ticks"] >= 2
    monkeypatch.setattr(gl, "mod_ready", ready)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    assert "мод ответил" in gl.launch(wait_seconds=30)


def test_crash_at_start_is_reported(monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(gl, "mod_ready", lambda: False)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="сразу закрылась"):
        gl.launch(wait_seconds=30)


def test_game_is_started_outside_the_bot_process_tree(monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    calls = []
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda args, **k: calls.append(args))
    monkeypatch.setattr(gl, "mod_ready", lambda: True)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    gl.launch(wait_seconds=10)
    assert calls[0][:3] == ["cmd.exe", "/c", "start"] and calls[0][-1] == str(exe)     # через start: игра не потомок бота
