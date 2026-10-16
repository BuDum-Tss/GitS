"""Всё, что конвейер берёт у ядра (ТЗ 20, раздел 11): тонкая обёртка над `gits_sdk.ModuleClient`.

Конвейер зависит от этого интерфейса, а не от клиента: тесты подставляют заглушку (`agent/tests/stubs.py`), живой прогон
идёт против FakeCore из SDK. Ошибки ядра приходят как `ToolError` с кодами ТЗ 01.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from gits_sdk import ModuleClient, ToolError

log = logging.getLogger("montage.core")

TTS_TIMEOUT_S = 120.0


class CoreAPI(Protocol):
    @property
    def settings(self) -> dict: ...
    @property
    def sleeping(self) -> bool: ...
    async def tts(self, text: str, emotion: str = "neutral") -> dict: ...
    async def persona(self) -> dict: ...
    async def claude_usage(self, tokens: int, source: str, limit_text: str | None = None) -> None: ...
    async def kb_ingest(self, collection: str, documents: list[dict], *, replace: bool = False, final: bool = False) -> dict: ...
    async def experience_add(self, entry: dict) -> dict: ...
    async def lookup(self, query: str, collections: list[str]) -> str: ...
    async def job_update(self, job_id: str, **fields) -> bool: ...
    async def event(self, kind: str, text: str, **fields) -> bool: ...
    async def resend_jobs(self) -> int: ...


class ClientCore:
    """Реализация поверх ModuleClient."""

    def __init__(self, client: ModuleClient):
        self.client = client

    @property
    def settings(self) -> dict:
        return self.client.module_settings

    @property
    def sleeping(self) -> bool:
        return bool((self.client.battery or {}).get("sleeping"))

    async def tts(self, text: str, emotion: str = "neutral") -> dict:
        """core.tts -> {"path", "duration_ms"}: синтез её голосом в файл (модуль голоса, ТЗ 12)."""
        try:
            res = await self.client.tts(text, emotion, "wav", timeout=TTS_TIMEOUT_S)
        except (KeyError, TypeError, ValueError) as e:
            raise ToolError("ядро вернуло неверный ответ на core.tts", code="internal") from e
        if not res.path:
            raise ToolError("ядро вернуло неверный ответ на core.tts", code="internal")
        return {"path": res.path, "duration_ms": res.duration_ms}

    async def persona(self) -> dict:
        return (await self.client.persona()).raw

    async def claude_usage(self, tokens: int, source: str, limit_text: str | None = None) -> None:
        try:
            await self.client.claude_usage(tokens, source, limit_text[:500] if limit_text else None)
        except (ToolError, asyncio.TimeoutError) as e:
            log.info("расход Claude не учтён ядром: %s", getattr(e, "message", None) or type(e).__name__)

    async def kb_ingest(self, collection: str, documents: list[dict], *, replace: bool = False, final: bool = False) -> dict:
        return await self.client.kb_ingest(collection, documents, replace=replace, final=final)

    async def experience_add(self, entry: dict) -> dict:
        return await self.client.experience_add(entry)

    async def lookup(self, query: str, collections: list[str]) -> str:
        return await self.client.lookup(query, collections, allow_web=False, k=5)

    async def job_update(self, job_id: str, **fields) -> bool:
        return await self.client.job_update(job_id, **fields)

    async def event(self, kind: str, text: str, **fields) -> bool:
        return await self.client.event(kind, text, **fields)

    async def resend_jobs(self) -> int:
        return await self.client.resend_jobs()
