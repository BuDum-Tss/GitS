"""Модуль монтажа в ядре (ТЗ 20, MON-34): запись в modules.yaml, поиск папки модуля, профиль editing."""
from pathlib import Path

import module_dirs
from profile_manager import load_profiles
from supervisor import Supervisor, load_planned, load_specs

TOKEN = "ab" * 32


def test_montage_dir_resolution_order(tmp_path):
    root = tmp_path / "GitS"
    root.mkdir()
    nowhere = str(tmp_path / "нет-такой-папки")
    assert module_dirs.resolve_montage_dir({}, root, nowhere) is None                         # ничего нет: «не настроен»
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    assert module_dirs.resolve_montage_dir({}, root, str(fallback)) == str(fallback)         # запасной путь, если он существует
    (tmp_path / "montage-module").mkdir()
    assert module_dirs.resolve_montage_dir({}, root, str(fallback)) == str(tmp_path / "montage-module")   # соседняя папка раньше запасной
    assert module_dirs.resolve_montage_dir({"MONTAGE_MODULE_DIR": r"D:\video"}, root, str(fallback)) == r"D:\video"   # переменная раньше всего


def test_apply_montage_sets_the_variable_for_modules_yaml(tmp_path):
    root = tmp_path / "GitS"
    root.mkdir()
    (tmp_path / "montage-module").mkdir()
    env = {}
    assert module_dirs.apply_montage(env, root, "") == str(tmp_path / "montage-module")
    assert env["MONTAGE_MODULE_DIR"] == str(tmp_path / "montage-module")
    lonely = tmp_path / "other" / "GitS"
    lonely.mkdir(parents=True)                                                             # рядом с этим GitS монтажа нет
    empty = {}
    assert module_dirs.apply_montage(empty, lonely, str(tmp_path / "нет")) is None and "MONTAGE_MODULE_DIR" not in empty


def test_modules_yaml_declares_the_montage_module():
    specs = {s.id: s for s in load_specs()}
    montage = specs["montage"]
    assert montage.cwd == "{MONTAGE_MODULE_DIR}/agent" and montage.command == "{MONTAGE_MODULE_DIR}/.venv/Scripts/python.exe"
    assert montage.autostart is True and montage.restart == "on-failure" and montage.kind == "module"
    assert "montage" not in {p.id for p in load_planned()}                                   # из «запланированных» убран


def test_montage_command_is_built_from_the_variable(tmp_path):
    repo = tmp_path / "montage-module"
    (repo / "agent").mkdir(parents=True)
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    python = repo / ".venv" / "Scripts" / "python.exe"
    python.write_text("", encoding="utf-8")
    sup = Supervisor(load_specs(), token=TOKEN, core_url="ws://x", env={"MONTAGE_MODULE_DIR": str(repo)})
    command, cwd = sup.resolve("montage")
    assert Path(command[0]) == python and command[1:] == ["-u", "main.py"] and Path(cwd) == repo / "agent"
    env = sup._process_env("montage")
    assert env["GITS_MODULE_ID"] == "montage" and env["GITS_MODULE_TOKEN"] == TOKEN
    unset = Supervisor(load_specs(), token=TOKEN, core_url="ws://x", env={})
    assert "MONTAGE_MODULE_DIR" in unset.resolve("montage")                                  # без переменной: «не настроен»


def test_editing_profile_is_real():
    editing = load_profiles()["editing"]
    assert editing.planned is False and editing.focus_modules == ("montage", "desktop")
    assert editing.memory_scopes == ("montage",)
