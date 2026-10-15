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
        value = await self.client.request("core.tts", {"text": text, "emotion": emotion, "format": "wav"}, timeout=TTS_TIMEOUT_S)
        if not isinstance(value, dict) or not value.get("path"):
            raise ToolError("ядро вернуло неверный ответ на core.tts", code="internal")
        return value

    async def persona(self) -> dict:
        value = await self.client.request("core.persona", {}, timeout=20)
        return value if isinstance(value, dict) else {}

    async def claude_usage(self, tokens: int, source: str, limit_text: str | None = None) -> None:
        body: dict = {"tokens": int(tokens), "source": source}
        if limit_text:
            body["limit_text"] = limit_text[:500]
        try:
            await self.client.request("core.claude_usage", body, timeout=15)
        except (ToolError, asyncio.TimeoutError) as e:
            log.info("расход Claude не учтён ядром: %s", getattr(e, "message", None) or type(e).__name__)

    async def kb_ingest(self, collection: str, documents: list[dict], *, replace: bool = False, final: bool = False) -> dict:
        body: dict = {"collection": collection, "documents": documents}
        if replace:
            body["replace"] = True
        if final:
            body["final"] = True
        return await self.client.request("core.kb.ingest", body, timeout=120)

    async def experience_add(self, entry: dict) -> dict:
        return await self.client.request("core.experience.add", entry, timeout=30)

    async def lookup(self, query: str, collections: list[str]) -> str:
        value = await self.client.request("core.lookup", {"query": query, "collections": collections, "allow_web": False, "k": 5},
                                          timeout=30)
        if isinstance(value, dict):
            return str(value.get("text") or "")
        return str(value or "")

    async def job_update(self, job_id: str, **fields) -> bool:
        return await self.client.job_update(job_id, **fields)

    async def event(self, kind: str, text: str, **fields) -> bool:
        return await self.client.event(kind, text, **fields)

    async def resend_jobs(self) -> int:
        return await self.client.resend_jobs()
