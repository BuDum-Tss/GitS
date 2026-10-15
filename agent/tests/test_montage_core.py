"""Модуль монтажа в ядре (ТЗ 20, MON-34): запись в modules.yaml, поиск папки модуля, профиль editing."""
from pathlib import Path

from profile_manager import load_profiles
from supervisor import Supervisor, load_planned, load_specs

TOKEN = "ab" * 32


def test_modules_yaml_declares_the_montage_module():
    specs = {s.id: s for s in load_specs()}
    montage = specs["montage"]
    assert montage.cwd == "{GITS}/modules/montage/agent" and montage.command == "{GITS}/modules/montage/.venv/Scripts/python.exe"
    assert montage.autostart is True and montage.restart == "on-failure" and montage.kind == "module"
    assert "montage" not in {p.id for p in load_planned()}                                   # из «запланированных» убран


def test_montage_command_is_built_from_the_repo_folder(tmp_path):
    repo = tmp_path / "modules" / "montage"
    (repo / "agent").mkdir(parents=True)
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    python = repo / ".venv" / "Scripts" / "python.exe"
    python.write_text("", encoding="utf-8")
    sup = Supervisor(load_specs(), token=TOKEN, core_url="ws://x", env={}, gits_root=tmp_path)
    command, cwd = sup.resolve("montage")
    assert Path(command[0]) == python and command[1:] == ["-u", "main.py"] and Path(cwd) == repo / "agent"
    env = sup._process_env("montage")
    assert env["GLADOS_MODULE_ID"] == "montage" and env["GLADOS_MODULE_TOKEN"] == TOKEN


def test_editing_profile_is_real():
    editing = load_profiles()["editing"]
    assert editing.planned is False and editing.focus_modules == ("montage", "desktop")
    assert editing.memory_scopes == ("montage",)
