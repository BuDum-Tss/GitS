"""Персонаж (ТЗ 05, CHR-5, CHR-6, CHR-8, CHR-10): загрузка карточки, список, пути памяти, неизменность конституции."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

import character
import prompts
import settings

CONSTITUTION_SHA256 = "bd95c53c07d006c81627569b0e253d2058a324a5bcfbcae98694edd54cf6bea2"   # CHR-5: sha256 json раздела constitution (sort_keys)


def _card(folder: Path, char_id: str, **over):
    folder.mkdir(parents=True, exist_ok=True)
    fields = {"id": char_id, "name": "Тест", "aliases": ["Тест", "тестовая"], "gender": "female", "language": "ru",
              "anchor": "Короткий якорь.", "voice": {"reference": "voice/x.wav"}}
    fields.update(over)
    import yaml
    (folder / "core").mkdir(exist_ok=True)
    (folder / "core" / "character.yaml").write_text(yaml.safe_dump(fields, allow_unicode=True), encoding="utf-8")
    return folder


@pytest.fixture
def chars(tmp_path, monkeypatch):
    """Отдельная папка персонажей: glados (настоящий) и tester (тестовый), шаблон `_template` в списке не виден."""
    root = tmp_path / "characters"
    shutil.copytree(Path(character.CHARACTERS_DIR) / "GlaDOS", root / "GlaDOS")
    _card(root / "tester", "tester", name="Тестерша")
    (root / "_template").mkdir()
    (root / "_template" / "core").mkdir(parents=True, exist_ok=True)
    (root / "_template" / "core" / "character.yaml").write_text("id: TODO_id\n", encoding="utf-8")
    monkeypatch.setattr(character, "CHARACTERS_DIR", root)
    return root


def test_glados_card_is_loaded_from_its_folder():
    ch = character.load("glados")
    assert ch.name == "ГЛаДОС" and ch.id == "glados" and ch.gender == "female"
    assert "гладос" in ch.aliases and "GLaDOS" in ch.aliases
    assert ch.folder.name == "GlaDOS" and ch.folder.parent.name == "resources"
    assert ch.voice_reference().is_file(), "референс голоса лежит в папке персонажа"
    assert len(ch.anchor) <= character.ANCHOR_MAX


def test_constitution_content_is_unchanged():
    """CHR-5: раздел `constitution` файла character.yaml не меняется (меняет только человек)."""
    path = Path(character.CHARACTERS_DIR) / "GlaDOS" / "core" / "character.yaml"
    section = yaml.safe_load(path.read_text(encoding="utf-8"))["constitution"]
    blob = json.dumps(section, sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(blob).hexdigest() == CONSTITUTION_SHA256


def test_list_skips_template_and_unfinished_folders(chars):
    assert character.list_ids() == ["glados", "tester"]   # шаблон _template и папки без character.yaml не видны


def test_unknown_character_is_an_error_with_a_reason(chars):
    with pytest.raises(character.CharacterError, match="нет папки персонажа"):
        character.load("nobody")
    with pytest.raises(character.CharacterError, match="недопустимый идентификатор"):
        character.load("../glados")


def test_folder_name_is_free_and_the_card_id_decides(chars):
    _card(chars / "any name", "other")
    assert character.load("other").folder.name == "any name"
    with pytest.raises(character.CharacterError, match="нет папки персонажа"):
        character.load("wrong")


def test_persona_sections_come_from_module_folders():
    sections = character.sections("glados")
    assert {"telegram_channel", "telegram_formats", "desktop_assistant"} <= set(sections)
    assert all(sections[k].strip() for k in sections)


def test_module_resources_live_in_the_module_folder_of_the_character():
    from gits_sdk import resources
    root = Path(character.CHARACTERS_DIR)
    assert resources.module_dir("pet", "glados") == root / "GlaDOS" / "pet"
    assert (resources.module_dir("pet", "glados") / "stickers" / "idle.png").is_file()
    assert (resources.module_dir("voice", "glados") / "GlaDos.wav").is_file()
    assert (resources.module_dir("montage", "glados") / "assets" / "fonts" / "OFL.txt").is_file()
    assert not (resources.module_dir("rimworld", "glados") / "prompts").exists()    # игровые промпты в modules/rimworld/prompts
    assert (resources.character_dir("glados") / "core" / "prompt.yaml").is_file()


def test_anchor_limit_is_enforced(chars):
    _card(chars / "long", "long", anchor="а" * (character.ANCHOR_MAX + 1))
    with pytest.raises(character.CharacterError, match="anchor длиннее"):
        character.load("long")


def test_name_is_required(chars):
    _card(chars / "noname", "noname", name="")
    with pytest.raises(character.CharacterError, match="нужно поле name"):
        character.load("noname")


def test_memory_dir_keeps_glados_at_root_and_moves_others(tmp_path, chars):
    root = tmp_path / "mem"
    assert character.memory_dir("glados", root) == root
    assert character.memory_dir("tester", root) == root / "characters" / "tester"


def test_active_follows_settings_and_falls_back_to_glados(chars):
    assert character.init().id == "glados"
    settings.update(character="tester")
    assert character.configured_id() == "tester"
    assert character.init().id == "tester"
    settings.update(character="missing")
    assert character.init().id == "glados", "битая настройка не роняет ядро: запасной glados"


def test_persist_writes_the_setting(chars):
    character.persist("tester")
    assert settings.load()["character"] == "tester"


def test_group_and_database_follow_the_scheme():
    """CHR-9 и ТЗ 04c WM-4: группа персонажа = id; область модуля = <id>:<модуль>; база = id персонажа."""
    assert character.group_id("glados") == "glados"
    assert character.group_id("glados", "rimworld") == "glados:rimworld"
    assert character.database_of("glados:rimworld") == "glados"
    assert character.database_of("tester") == "tester"


def test_persona_sections_are_parsed(chars):
    sections = character.sections("glados")
    assert "telegram_channel" in sections and "desktop_assistant" in sections
    assert sections["telegram_channel"].startswith("Ты — ГЛаДОС")
    assert character.sections("nobody") == {}


def test_persona_fallback_uses_the_character_name(chars):
    """CHR-4: у персонажа без prompt.yaml запасной текст собирается из его имени, без зашитого имени ГЛаДОС."""
    character.set_active("tester")                # в папке tester нет prompt.yaml и раздела constitution
    prompts.reload()
    assert "Тестерша" in prompts.persona_card()
    assert "ГЛаДОС" not in prompts.companion_persona()


def test_persona_name_stays_out_of_agent_code():
    """CHR-4, CHR-13 (сторож): в agent/**/*.py (кроме characters/, тестов и копии envcompat) нет имени персонажа без
    пометки `# id данных`. Текст для модели и имена в интерфейсе берутся из карточки персонажа."""
    import re
    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"гладос|глэдос|glados|aperture", re.IGNORECASE)
    offenders = []
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if "resources" in parts or "tests" in parts or "__pycache__" in parts or path.name == "envcompat.py":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "# id данных" not in line:
                offenders.append(f"{'/'.join(parts)}:{n}: {line.strip()[:100]}")
    assert offenders == [], "имя персонажа в коде:\n" + "\n".join(offenders)
