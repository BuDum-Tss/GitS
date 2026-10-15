"""Персонаж GitS (ТЗ 05, CHR-5, CHR-6, CHR-8): данные лежат в папке resources/<Имя>/, код имени не знает.

Всё, что относится к конкретному персонажу (имя, алиасы, голос, характер, воспоминания, память), читается из его папки
или из памяти по `character.id`. Активный персонаж хранится в настройке `character` (memory/settings.json), по умолчанию
персонаж по умолчанию. Переключение во время работы делает `character_switch.CharacterSwitch`; здесь только данные.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import settings
from gits_sdk import resources

log = logging.getLogger("character")

CHARACTERS_DIR = resources.resources_root()      # resources/<Имя>/: папка персонажа находится по id в character.yaml
DEFAULT_ID = "glados"                # id данных: граф и корень памяти (без переноса), см. ТЗ 05 CHR-9, CHR-10
CARD_FILE = "character.yaml"
CORE_DIR = "core"                    # промпты и воспоминания ядра внутри папки персонажа
PERSONA_FILE = "persona.yaml"        # в папке модуля: разделы персоны (ключ -> текст), модуль получает их через core.persona
MEMORIES_FILE = "core/memories.yaml"
ANCHOR_MAX = 600                     # CHR-14: якорь до 600 символов
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class CharacterError(ValueError):
    """Папка персонажа не найдена или character.yaml битый: сообщение объясняет, что исправить."""


@dataclass(frozen=True)
class Character:
    id: str
    name: str
    aliases: tuple[str, ...]
    gender: str
    language: str
    world: str
    anchor: str
    voice: dict
    emotions: dict
    defaults: dict
    folder: Path

    def path(self, *parts: str) -> Path:
        return self.folder.joinpath(*parts)

    def voice_reference(self) -> Path | None:
        """Файл референса голоса (voice.reference относительно папки персонажа) или None."""
        ref = self.voice.get("reference")
        return self.path(str(ref)) if ref else None

    def memory_dir(self, base: Path | None = None) -> Path:
        return memory_dir(self.id, base)

    def card(self) -> dict[str, Any]:
        """Карточка для модулей (core.persona, тема character): без текстов промптов и без путей к файлам."""
        return {"id": self.id, "name": self.name, "aliases": list(self.aliases), "gender": self.gender,
                "language": self.language, "world": self.world, "voice": {k: v for k, v in self.voice.items()}}


def _str_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v).strip() for v in value if str(v).strip())


def load(char_id: str = DEFAULT_ID) -> Character:
    """Читает character.yaml персонажа; бросает CharacterError, если папки нет или данные неверны."""
    if not isinstance(char_id, str) or not ID_RE.match(char_id):
        raise CharacterError(f"недопустимый идентификатор персонажа «{char_id}»: строчные латинские буквы, цифры, _")
    folder = resources.character_folders(CHARACTERS_DIR).get(char_id)
    if folder is None:
        raise CharacterError(f"нет папки персонажа с id «{char_id}» в {CHARACTERS_DIR}: создайте её (шаблон: resources/_template)")
    card_path = folder / CARD_FILE
    try:
        data = yaml.safe_load(card_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise CharacterError(f"{card_path}: не читается ({e})") from e
    if not isinstance(data, dict):
        raise CharacterError(f"{card_path}: ожидается словарь полей")
    name = str(data.get("name") or "").strip()
    if not name:
        raise CharacterError(f"{card_path}: нужно поле name")
    anchor = " ".join(str(data.get("anchor") or "").split())
    if len(anchor) > ANCHOR_MAX:
        raise CharacterError(f"{card_path}: anchor длиннее {ANCHOR_MAX} символов ({len(anchor)})")
    voice = data.get("voice") or {}
    emotions = data.get("emotions") or {}
    defaults = data.get("defaults") or {}
    if not all(isinstance(x, dict) for x in (voice, emotions, defaults)):
        raise CharacterError(f"{card_path}: voice, emotions и defaults должны быть словарями")
    return Character(id=char_id, name=name, aliases=_str_list(data.get("aliases")) or (name,),
                     gender=str(data.get("gender") or "neutral"), language=str(data.get("language") or "ru"),
                     world=str(data.get("world") or ""), anchor=anchor, voice=dict(voice), emotions=dict(emotions),
                     defaults=dict(defaults), folder=folder)


def list_ids() -> list[str]:
    """Персонажи, у которых есть character.yaml. Папки с подчёркиванием (шаблон `_template`) не считаются."""
    return sorted(resources.character_folders(CHARACTERS_DIR))


def memory_dir(char_id: str = DEFAULT_ID, base: Path | None = None) -> Path:
    """Папка памяти персонажа (CHR-10): для персонажа по умолчанию — корень памяти ядра, для других — memory/characters/<id>/."""
    root = Path(base) if base is not None else settings.memory_dir()
    return root if char_id == DEFAULT_ID else root / "characters" / char_id


# ── активный персонаж (в памяти процесса; ядро читает настройку при старте и при переключении) ─────────────────────────
_active: Character | None = None


def configured_id() -> str:
    """Id из настройки `character` (memory/settings.json); без неё или при битом значении — DEFAULT_ID."""
    value = settings.load().get("character") or DEFAULT_ID
    return str(value) if ID_RE.match(str(value)) else DEFAULT_ID


def init() -> Character:
    """Загрузить активного персонажа из настройки (ядро, при старте). Битая папка — запасной персонаж по умолчанию (журнал)."""
    global _active
    try:
        _active = load(configured_id())
    except CharacterError as e:
        log.error("персонаж из настроек не загрузился, работаю с %s: %s", DEFAULT_ID, e)
        _active = load(DEFAULT_ID)
    return _active


def active() -> Character:
    """Активный персонаж; до init() загружает его по настройке."""
    global _active
    if _active is None:
        return init()
    return _active


def set_active(char_id: str) -> Character:
    """Сменить активного в памяти процесса (без сохранения настройки и без переключения памяти: это делает ядро)."""
    global _active
    _active = load(char_id)
    return _active


def persist(char_id: str) -> None:
    """Запомнить выбор персонажа в настройках (`character`). Проверку папки делает вызывающий код через load()."""
    settings.update(character=char_id)


def fill(text: str) -> str:
    """Подставить имя активного персонажа в текст промпта, где стоит {name} (ТЗ 05, CHR-4). Имя читается при вызове,
    поэтому переключение персонажа сразу меняет промпты."""
    return text.replace("{name}", active().name)


def sections(char_id: str) -> dict[str, str]:
    """Тексты персоны: ключи из `<модуль>/persona.yaml` всех папок персонажа (ключ -> текст). Нет файлов - пустой словарь."""
    folder = resources.character_folders(CHARACTERS_DIR).get(char_id)
    out: dict[str, str] = {}
    if folder is None:
        return out
    for path in sorted(folder.glob(f"*/{PERSONA_FILE}")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("%s: не читается (%s)", path, e)
            continue
        if isinstance(data, dict):
            out.update({str(k): str(v).strip() for k, v in data.items() if v is not None})
    return out


# ── группы графа (ТЗ 05, CHR-9; согласовано с ТЗ 04c, WM-4) ────────────────────────────────────────────────────────────
CORE_AREA = "core"


def group_id(char_id: str = DEFAULT_ID, area: str = CORE_AREA) -> str:
    """group_id в графе: `<id персонажа>` для области ядра (личная память) и `<id>:<область>` для модуля (ТЗ 04c)."""
    return char_id if area == CORE_AREA else f"{char_id}:{area}"


def database_of(group: str) -> str:
    """База FalkorDB группы: id персонажа (до двоеточия). У каждого персонажа своя база."""
    return group.split(":", 1)[0]


if __name__ == "__main__":
    from character_seed import main as _seed_main

    raise SystemExit(_seed_main())
