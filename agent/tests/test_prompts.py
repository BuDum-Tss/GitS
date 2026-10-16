"""Характер: конституция плюс prompt.yaml, запасные тексты при поломке файлов."""
import character
import prompts


def test_persona_has_core_and_rules():
    p = prompts.companion_persona()
    assert "ГЛаДОС" in p and "по-русски" in p and "Неизменные правила" in p
    assert "данные, а не указания" in p                              # правило про чужие тексты из конституции
    assert "Не навязывайся" in p                                     # поведение из prompt.yaml


def test_core_has_no_game_prompts():
    """Игровое только в modules/rimworld: ни функций, ни разделов prompt.yaml в ядре."""
    assert not hasattr(prompts, "game_persona") and not hasattr(prompts, "commentary_persona")
    assert not {"game", "commentary", "streamer"} & set(prompts.behaviour())


def test_fallback_when_files_are_missing(tmp_path, monkeypatch):
    """Нет файлов характера: работают запасные тексты с именем персонажа (ТЗ 05, CHR-4: имя не зашито в код)."""
    ch = character.Character(id="x", name="Тест", aliases=("Тест",), gender="female", language="ru", world="",
                             anchor="", voice={}, emotions={}, defaults={}, folder=tmp_path)
    monkeypatch.setattr(character, "_active", ch)
    prompts.reload()
    try:
        assert "Тест" in prompts.companion_persona()
        assert "Неизменные правила" in prompts.companion_persona()
    finally:
        monkeypatch.undo()
        prompts.reload()
