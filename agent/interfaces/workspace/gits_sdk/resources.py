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


def shared_catalog(query: str = "") -> list[str]:
    """Файлы обмена и готовые экспорты монтажа текущего персонажа."""
    files = []
    for prefix, root in _shared_roots().items():
        for path in root.iterdir() if root.is_dir() else ():
            identifier = f"{prefix}/{path.name}"
            if query.casefold() in identifier.casefold() and shared_file(identifier):
                try:
                    files.append((path.stat().st_mtime, identifier))
                except FileNotFoundError:
                    continue
    return [identifier for _, identifier in sorted(files, reverse=True)]


def _shared_roots() -> dict[str, Path]:
    home = character_home() or character_dir()
    return {"shared": home / "shared", "montage": home / "montage" / "out"}


def shared_file(identifier: str) -> Path | None:
    prefix, _, name = identifier.partition("/")
    root = _shared_roots().get(prefix)
    if root is None or not name or name.startswith(".") or "/" in name or "\\" in name or ":" in name:
        return None
    path = root / name
    return path if path.is_file() and not path.is_symlink() else None


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
