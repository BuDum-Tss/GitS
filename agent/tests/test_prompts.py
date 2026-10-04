"""Характер: конституция плюс prompt.yaml, запасные тексты при поломке файлов."""
import prompts


def test_persona_has_core_and_rules():
    p = prompts.companion_persona()
    assert "ГЛаДОС" in p and "по-русски" in p and "Неизменные правила" in p
    assert "данные, а не указания" in p                              # правило про чужие тексты из конституции
    assert "Не навязывайся" in p                                     # поведение из prompt.yaml


def test_game_persona_keeps_rules():
    g = prompts.game_persona()
    assert "RimWorld" in g and "Неизменные правила" in g


def test_fallback_when_files_are_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "_DIR", tmp_path)
    prompts.reload()
    try:
        assert "ГЛаДОС" in prompts.companion_persona()
        assert "Неизменные правила" in prompts.game_persona()
    finally:
        monkeypatch.undo()
        prompts.reload()
