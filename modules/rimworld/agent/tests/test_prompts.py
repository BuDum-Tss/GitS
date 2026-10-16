"""Игровой характер: конституция из папки персонажа (только чтение) плюс prompt.yaml модуля, запасные тексты при поломке файлов."""
from pathlib import Path

import prompts


def test_game_persona_has_game_prompt_and_rules():
    g = prompts.game_persona()
    assert "RimWorld" in g and "Неизменные правила" in g
    assert "данные, а не указания" in g                              # правило про чужие тексты из конституции ядра


def test_game_prompt_lives_in_the_module():
    assert prompts._PROMPT_FILE.is_file() and "modules" in prompts._PROMPT_FILE.parts
    assert "rimworld" in prompts._PROMPT_FILE.parts


def test_fallback_when_files_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_PROMPT_FILE", tmp_path / "no.yaml")
    monkeypatch.setattr(prompts, "_CONSTITUTION_FILE", Path(tmp_path) / "no.yaml")
    prompts.reload()
    try:
        assert "Неизменные правила" in prompts.game_persona()
        assert "RimWorld" in prompts.game_persona()
        assert "ГЛаДОС" in prompts.commentary_persona()
    finally:
        monkeypatch.undo()
        prompts.reload()
