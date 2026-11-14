"""Фокус среды: что в текущей среде видно и как звучит (бывшие профили).

Среду выбирает `WorkspaceManager`; здесь для текущей среды берутся тон реплик, модули, чьи инструменты видны планировщику
(`availability: profile`), и области памяти графа. Данные лежат в `space_config.yaml` рядом: ключ это id среды (`assistant`,
`sleep` или id модуля). У среды модуля без записи тона нет, виден сам модуль и открыта его область памяти.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

logger = logging.getLogger("spaces.focus")

CONFIG_FILE = Path(__file__).resolve().with_name("space_config.yaml")


@dataclass(frozen=True)
class SpaceConfig:
    tone: str = ""
    focus: tuple[str, ...] = ()
    memory_scopes: tuple[str, ...] = ()


def load_config(path: Path | str = CONFIG_FILE) -> tuple[dict[str, SpaceConfig], str]:
    """({id среды: настройки}, добавка тона стримера). Битый файл даёт пустой результат: фокус не должен ронять ядро."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning(f"Настройки сред не прочитаны: {e}")
        return {}, ""
    spaces = {}
    for sid, raw in (data.get("spaces") or {}).items():
        raw = raw or {}
        spaces[str(sid)] = SpaceConfig(tone=" ".join(str(raw.get("tone") or "").split()),
                                       focus=tuple(str(x) for x in raw.get("focus") or ()),
                                       memory_scopes=tuple(str(x) for x in raw.get("memory_scopes") or ()))
    return spaces, " ".join(str(data.get("streamer_tone") or "").split())


class SpaceFocus:
    def __init__(self, workspaces, config: dict[str, SpaceConfig] | None = None, streamer_tone: str | None = None, *,
                 streamer: Callable[[], bool] = lambda: False):
        """workspaces - WorkspaceManager (`current`, `specs`); streamer() - включён ли режим стримера."""
        loaded, tone = load_config() if config is None else (config, streamer_tone or "")
        self._ws, self.config, self._streamer_tone, self._streamer = workspaces, loaded, tone, streamer

    def _cfg(self, ws: str | None = None) -> SpaceConfig:
        ws = ws or self._ws.current
        cfg = self.config.get(ws)
        return cfg if cfg is not None else SpaceConfig(focus=(ws,), memory_scopes=(ws,))

    def focus_modules(self) -> set[str]:
        """Модули, чьи инструменты видны планировщику в текущей среде (`Registry.focus`)."""
        return set(self._cfg().focus)

    def memory_scopes(self) -> list[str]:
        return list(self._cfg().memory_scopes)

    def scope_title(self, scope: str) -> str:
        """Описание области памяти: название среды, которой она открыта, иначе сама область."""
        for ws, cfg in self.config.items():
            if scope in cfg.memory_scopes and ws in self._ws.specs:
                return self._ws.specs[ws].purpose or self._ws.specs[ws].title
        return scope

    def info(self) -> dict:
        """Для темы `profile` и `welcome` участникам: {"id", "title", "focus_modules"}."""
        ws = self._ws.current
        spec = self._ws.specs.get(ws)
        return {"id": ws, "title": spec.title if spec else ws, "focus_modules": sorted(self._cfg().focus)}

    def hint(self) -> str:
        """Подсказка тона в промпт разговора: тон текущей среды и добавка стримера."""
        tone = self._cfg().tone
        if self._streamer() and self._streamer_tone:
            tone = f"{tone} {self._streamer_tone}".strip()
        return (f"[Внутренняя настройка тона, не тема разговора и не повод что-то сообщать: {tone} "
                "Никогда не упоминай режимы, профили и их переключение.]")
