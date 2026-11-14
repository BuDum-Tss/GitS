"""Всё, что конвейер берёт у ядра (ТЗ 20, раздел 11): тонкая обёртка над `gits_sdk.WorkspaceClient`.

Конвейер зависит от этого интерфейса, а не от клиента: тесты подставляют заглушку (`agent/tests/stubs.py`), живой прогон
идёт против FakeCore из SDK. Ошибки ядра приходят как `ToolError` с кодами ТЗ 01.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gits_sdk import WorkspaceClient, ToolError

log = logging.getLogger("montage.core")

TTS_TIMEOUT_S = 120.0


class CoreAPI(Protocol):
    @property
    def settings(self) -> dict: ...
    async def tts(self, text: str, emotion: str = "neutral") -> dict: ...
    async def persona(self) -> dict: ...
    async def job_update(self, job_id: str, **fields) -> bool: ...
    async def event(self, kind: str, text: str, **fields) -> bool: ...
    async def resend_jobs(self) -> int: ...
    async def tasks(self) -> dict: ...


class ClientCore:
    """Реализация поверх WorkspaceClient."""

    def __init__(self, client: WorkspaceClient, volume: Callable[[], Path | None] = lambda: None):
        self.client = client
        self._volume = volume

    @property
    def settings(self) -> dict:
        return self.client.module_settings

    @property
    def runtime_settings(self) -> dict:
        """Общие настройки ядра (`shared_settings` манифеста): `pi_model_heavy`, `pi_thinking`."""
        return self.client.settings

    async def tts(self, text: str, emotion: str = "neutral") -> dict:
        """core.tts -> {"path", "duration_ms"}: синтез её голосом в файл (модуль голоса, ТЗ 12)."""
        try:
            res = await self.client.tts(text, emotion, "wav", timeout=TTS_TIMEOUT_S)
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError("ядро вернуло неверный ответ на core.tts", code="internal") from e
        shared = self._volume_file(res.volume_path)
        if shared is not None:                      # общий том персонажа: тот же файл, но по пути этого контейнера
            return {"path": str(shared), "duration_ms": res.duration_ms}
        if not res.path:
            raise ToolError("ядро вернуло неверный ответ на core.tts", code="internal")
        return {"path": res.path, "duration_ms": res.duration_ms}

    def _volume_file(self, rel: str | None) -> Path | None:
        """Файл из общего тома персонажа по пути относительно его папки; нет такого или путь выходит за том: None."""
        root = self._volume()
        if not rel or root is None:
            return None
        parts = rel.replace("\\", "/").lstrip("/").split("/")
        if ".." in parts:
            return None
        p = Path(root).joinpath(*parts)
        return p if p.is_file() else None

    async def persona(self) -> dict:
        return (await self.client.persona()).raw

    async def job_update(self, job_id: str, **fields) -> bool:
        return await self.client.job_update(job_id, **fields)

    async def event(self, kind: str, text: str, **fields) -> bool:
        return await self.client.event(kind, text, **fields)

    async def resend_jobs(self) -> int:
        return await self.client.resend_jobs()

    async def tasks(self) -> dict:
        """core.tasks: задачи текущей среды (цель, шаги, прогресс) из дерева задач ядра."""
        return await self.client.tasks()
