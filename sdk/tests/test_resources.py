"""Ресурсы персонажа: resources/<Имя>/<модуль>/ (папка находится по id в character.yaml)."""
import pytest

from gits_sdk import resources


def _card(folder, cid):
    folder.mkdir(parents=True)
    (folder / "core").mkdir(exist_ok=True)
    (folder / "core" / "character.yaml").write_text(f"# карточка\nid: {cid}            # id\nname: X\n", encoding="utf-8")


def test_folder_is_found_by_card_id_not_by_name(tmp_path):
    _card(tmp_path / "Any Name", "tester")
    _card(tmp_path / "_template", "TODO_id")
    assert resources.character_folders(tmp_path) == {"tester": tmp_path / "Any Name"}
    assert resources.module_dir("pet", "tester", tmp_path) == tmp_path / "Any Name" / "pet"


def test_unknown_character_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="нет персонажа"):
        resources.character_dir("nobody", tmp_path)


def test_default_character_comes_from_the_environment(tmp_path, monkeypatch):
    _card(tmp_path / "A", "first")
    _card(tmp_path / "B", "second")
    monkeypatch.setenv("GITS_CHARACTER_ID", "second")
    assert resources.character_dir(root=tmp_path) == tmp_path / "B"
    monkeypatch.setenv("GITS_RESOURCES_DIR", str(tmp_path))
    assert resources.resources_root() == tmp_path
