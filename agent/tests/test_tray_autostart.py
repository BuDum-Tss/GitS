"""Запись автозапуска трея: протухший путь (проект переехал, Python обновили) определяется, живой не считается протухшим."""
from pathlib import Path

import tray_autostart as ta


def test_command_roundtrip(tmp_path):
    py, script = tmp_path / "pythonw.exe", tmp_path / "tray.py"
    assert ta.parts(ta.command(str(py), script)) == (str(py), str(script))


def test_alive_entry_is_not_stale(tmp_path):
    py, script = tmp_path / "pythonw.exe", tmp_path / "tray.py"
    py.write_bytes(b"")
    script.write_text("", encoding="utf-8")
    assert ta.is_stale(ta.command(str(py), script)) is False


def test_missing_script_or_interpreter_or_foreign_format_is_stale(tmp_path):
    py, script = tmp_path / "pythonw.exe", tmp_path / "tray.py"
    py.write_bytes(b"")
    assert ta.is_stale(ta.command(str(py), script)) is True                 # tray.py нет (проект переехал)
    script.write_text("", encoding="utf-8")
    py.unlink()
    assert ta.is_stale(ta.command(str(py), script)) is True                 # Python удалён или обновлён
    assert ta.is_stale("tray.exe --silent") is True and ta.is_stale(None) is True


def test_old_project_path_from_the_registry_is_stale():
    old = r'"C:\Users\x\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe" "C:\MyProjects\rimworld-agent\agent\tray.py"'
    assert ta.parts(old)[1].endswith("tray.py") and not Path(ta.parts(old)[1]).exists()
