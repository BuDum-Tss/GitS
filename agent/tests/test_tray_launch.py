"""Трей стартует вместе с ядром (tray_launch.py). Процессов не создаём: spawn подменён."""
from pathlib import Path

import tray_launch as tl


def test_wanted_only_on_windows_and_unless_switched_off():
    assert tl.wanted({}, "win32")
    assert not tl.wanted({}, "linux")
    for off in ("1", "true", "YES", "on"):
        assert not tl.wanted({tl.NO_TRAY_ENV: off}, "win32"), off
    assert tl.wanted({tl.NO_TRAY_ENV: "0"}, "win32")


def test_command_prefers_pythonw_next_to_the_interpreter(tmp_path):
    (tmp_path / "python.exe").write_text("")
    assert tl.command(str(tmp_path / "python.exe"), tmp_path)[0].endswith("python.exe")
    (tmp_path / "pythonw.exe").write_text("")
    cmd = tl.command(str(tmp_path / "python.exe"), tmp_path)
    assert cmd[0].endswith("pythonw.exe") and Path(cmd[1]) == tmp_path / "tray.py"


def test_ensure_tray_spawns_detached_and_survives_failures(tmp_path):
    (tmp_path / "tray.py").write_text("")
    calls = []

    def spawn(cmd, **kw):
        calls.append((cmd, kw))

    assert tl.ensure_tray(env={}, platform="win32", executable=str(tmp_path / "python.exe"), here=tmp_path, spawn=spawn)
    cmd, kw = calls[0]
    assert cmd[-1].endswith("tray.py") and kw["cwd"] == str(tmp_path) and kw["creationflags"] & 0x00000008   # отдельный процесс

    assert not tl.ensure_tray(env={tl.NO_TRAY_ENV: "1"}, platform="win32", here=tmp_path, spawn=spawn)
    assert not tl.ensure_tray(env={}, platform="linux", here=tmp_path, spawn=spawn)
    assert not tl.ensure_tray(env={}, platform="win32", here=tmp_path / "нет", spawn=spawn)
    assert len(calls) == 1

    def broken(cmd, **kw):
        raise OSError("нет такого файла")

    assert not tl.ensure_tray(env={}, platform="win32", here=tmp_path, spawn=broken)       # сбой не роняет ядро
