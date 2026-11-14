"""Ресурсы персонажа: `resources/<Имя>/<модуль>/` (папка модуля внутри папки персонажа).

Всё, что связано с персонажем (промпты, референс голоса, стикеры питомца, шрифты и звуки монтажа), лежит в `resources/<Имя>/`:
`core/` ядра и по папке на модуль (`voice/`, `pet/`, `telegram/`, `desktop/`, `rimworld/`, `montage/`). Модуль берёт ресурсы только
из своей папки: `module_dir("pet")`. Папка персонажа находится по `id` в её `core/character.yaml`, имя папки произвольное.

Корень ресурсов: переменная `GITS_RESOURCES_DIR` или `resources/` рядом с `agent/interfaces/workspace/` (репозиторий GitS). Персонаж по умолчанию:
`GITS_CHARACTER_ID` (его выставляет супервизор при запуске модуля), иначе `DEFAULT_ID`. Модуль не импортирует `agent/`, поэтому
`id` читается из core/character.yaml простым разбором строки без yaml.

Память персонажа лежит в его папке: `resources/<Имя>/memory/` (`default_memory_dir`).

Том персонажа (Docker, один контейнер на персонажа): переменная `CHARACTER_HOME` (абсолютный путь) задаёт саму папку персонажа,
в ней лежит `core/character.yaml`, ресурсы модулей (`<модуль>/`) и память ядра в `memory/`. Без переменной всё работает как раньше.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_ID = "glados"
CARD_FILE = "core/character.yaml"
_ID_LINE = re.compile(r"^id:\s*['\"]?([A-Za-z0-9_]+)['\"]?\s*(?:#.*)?$", re.M)


def resources_root() -> Path:
    env = os.environ.get("GITS_RESOURCES_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[4] / "resources"


def character_home() -> Path | None:
    """Том персонажа из `CHARACTER_HOME` или None (переменная не задана)."""
    env = os.environ.get("CHARACTER_HOME", "").strip()
    return Path(env) if env else None


def home_memory_dir() -> Path | None:
    """Папка памяти тома: `CHARACTER_HOME/memory` или None."""
    home = character_home()
    return home / "memory" if home is not None else None


def default_memory_dir() -> Path:
    """Папка памяти: `MEMORY_DIR`; иначе `CHARACTER_HOME/memory` (том); иначе `resources/<персонаж>/memory` (персонаж из
    `GITS_CHARACTER_ID`, по умолчанию `DEFAULT_ID`); нет папки персонажа: `./memory`."""
    env = os.environ.get("MEMORY_DIR", "").strip()
    if env:
        return Path(env)
    home = home_memory_dir()
    if home is not None:
        return home
    folder = character_folders().get(os.environ.get("GITS_CHARACTER_ID") or DEFAULT_ID)
    return folder / "memory" if folder is not None else Path("./memory")


_PRIVATE_PARTS = {"core", "memory", "settings", "config", "secrets", "credentials", "node_modules", "__pycache__"}
_PRIVATE_FILES = {"settings.json", "settings.yaml", "settings.yml", "persona.yaml", "persona.yml",
                  "config.json", "config.yaml", "config.yml", "character.yaml", "prompt.yaml",
                  "auth.json", "credentials.json", "tokens.env", "twitch_tokens.json", "module_token"}
_PRIVATE_SUFFIXES = {".env", ".session", ".session-journal", ".pem", ".key"}


def _resource_home() -> Path:
    return character_home() or character_dir()


def _public_part(part: str) -> bool:
    name = part.casefold()
    return (bool(name) and name == name.rstrip(" .") and not name.startswith(".")
            and chr(92) not in name and ":" not in name
            and name not in _PRIVATE_PARTS and name not in _PRIVATE_FILES
            and Path(name).suffix not in _PRIVATE_SUFFIXES)


def resource_path(identifier: str, *, home: Path | None = None) -> Path | None:
    home = home if home is not None else _resource_home()
    parts = identifier.split("/") if identifier else []
    if not all(_public_part(part) for part in parts):
        return None
    path = home.joinpath(*parts)
    try:
        if path.is_symlink() or path.resolve() != home.resolve().joinpath(*parts):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def shared_entries(folder: str = "") -> list[dict] | None:
    root = resource_path(folder)
    if root is None or not root.is_dir():
        return None
    entries = []
    for child in root.iterdir():
        identifier = f"{folder}/{child.name}" if folder else child.name
        if resource_path(identifier) is not None and (child.is_file() or child.is_dir()):
            entries.append({"name": child.name, "path": identifier, "directory": child.is_dir()})
    return sorted(entries, key=lambda row: (not row["directory"], row["name"].casefold()))


def shared_catalog(query: str = "") -> list[str]:
    """Ресурсы текущего персонажа. При равном фильтре готовые экспорты первыми."""
    folders = [""]
    files = []
    while folders:
        for entry in shared_entries(folders.pop()) or ():
            identifier = entry["path"]
            if entry["directory"]:
                folders.append(identifier)
            elif query.casefold() in identifier.casefold():
                try:
                    files.append((identifier.startswith("montage/out/"), (_resource_home() / identifier).stat().st_mtime, identifier))
                except FileNotFoundError:
                    continue
    return [identifier for _, _, identifier in sorted(files, reverse=True)]


def shared_file(identifier: str) -> Path | None:
    path = resource_path(identifier)
    if path is not None and path.is_file():
        return path
    # Старые идентификаторы экспортов имели вид montage/<имя>.
    if identifier.startswith("montage/") and not identifier.startswith("montage/out/"):
        path = resource_path("montage/out/" + identifier.removeprefix("montage/"))
        if path is not None and path.is_file():
            return path
    return None


def card_id(folder: Path) -> str | None:
    """id из core/character.yaml папки или None."""
    try:
        text = (folder / CARD_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _ID_LINE.search(text)
    return m.group(1) if m else None


def character_folders(root: Path | None = None) -> dict[str, Path]:
    """id -> папка для всех персонажей (папки с подчёркиванием, например шаблон, не считаются)."""
    if root is None:
        home = character_home()
        if home is not None:
            cid = card_id(home)
            return {cid: home} if cid else {}
    root = Path(root) if root is not None else resources_root()
    out: dict[str, Path] = {}
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if p.is_dir() and not p.name.startswith("_"):
                cid = card_id(p)
                if cid:
                    out[cid] = p
    return out


def character_dir(char_id: str | None = None, root: Path | None = None) -> Path:
    """Папка персонажа. Нет такого — FileNotFoundError с понятным текстом."""
    home = character_home()
    if home is not None and root is None:
        cid = card_id(home)
        if cid is None:
            raise FileNotFoundError(f"CHARACTER_HOME={home}: нет {CARD_FILE} с id")
        if char_id and char_id != cid:
            raise FileNotFoundError(f"в томе {home} персонаж «{cid}», а не «{char_id}»")
        return home
    cid = char_id or os.environ.get("GITS_CHARACTER_ID") or DEFAULT_ID
    folders = character_folders(root)
    if cid not in folders:
        raise FileNotFoundError(f"нет персонажа «{cid}» в {root or resources_root()} (папка с core/character.yaml, id: {cid})")
    return folders[cid]


def module_dir(peer_id: str, char_id: str | None = None, root: Path | None = None) -> Path:
    """Папка ресурсов модуля у персонажа: `resources/<Имя>/<модуль>/`. Не создаётся."""
    return character_dir(char_id, root) / peer_id
