"""Ресурсы персонажа: `resources/<Имя>/<модуль>/` (папка модуля внутри папки персонажа).

Всё, что связано с персонажем (промпты, референс голоса, стикеры питомца, шрифты и звуки монтажа), лежит в `resources/<Имя>/`:
`core/` ядра и по папке на модуль (`voice/`, `pet/`, `telegram/`, `desktop/`, `rimworld/`, `montage/`). Модуль берёт ресурсы только
из своей папки: `module_dir("pet")`. Папка персонажа находится по `id` в её `character.yaml`, имя папки произвольное.

Корень ресурсов: переменная `GITS_RESOURCES_DIR` или `resources/` рядом с `sdk/` (репозиторий GitS). Персонаж по умолчанию:
`GITS_CHARACTER_ID` (его выставляет супервизор при запуске модуля), иначе `DEFAULT_ID`. Модуль не импортирует `agent/`, поэтому
`id` читается из character.yaml простым разбором строки без yaml.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULT_ID = "glados"
CARD_FILE = "character.yaml"
_ID_LINE = re.compile(r"^id:\s*['\"]?([A-Za-z0-9_]+)['\"]?\s*(?:#.*)?$", re.M)


def resources_root() -> Path:
    env = os.environ.get("GITS_RESOURCES_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "resources"


def card_id(folder: Path) -> str | None:
    """id из character.yaml папки или None."""
    try:
        text = (folder / CARD_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    m = _ID_LINE.search(text)
    return m.group(1) if m else None


def character_folders(root: Path | None = None) -> dict[str, Path]:
    """id -> папка для всех персонажей (папки с подчёркиванием, например шаблон, не считаются)."""
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
    cid = char_id or os.environ.get("GITS_CHARACTER_ID") or DEFAULT_ID
    folders = character_folders(root)
    if cid not in folders:
        raise FileNotFoundError(f"нет персонажа «{cid}» в {root or resources_root()} (папка с character.yaml, id: {cid})")
    return folders[cid]


def module_dir(module_id: str, char_id: str | None = None, root: Path | None = None) -> Path:
    """Папка ресурсов модуля у персонажа: `resources/<Имя>/<модуль>/`. Не создаётся."""
    return character_dir(char_id, root) / module_id
