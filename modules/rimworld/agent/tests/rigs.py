"""Стенд тестов модуля: настоящий ModuleClient модуля RimWorld против FakeCore (поддельное ядро SDK).

Без игры, без сети (кроме локального aiohttp на свободном порту), без Claude: функции MCP-сервера подменяются в тестах."""
import asyncio
import contextlib

from gits_sdk import ModuleClient
from gits_sdk.testing import FakeCore

import game_api
import module_manifest
import settings
from core_link import CoreLink

SHARED = {"streamer": False, "dnd": False, "quiet_until": 0.0, "game_model": "haiku", "claude_usage": "normal",
          "voice_muted": False, "telegram_after_run": False, "record_runs": False}


class Rig:
    def __init__(self, tmp_path, shared=None, module_settings=None, order=None):
        self.tmp = tmp_path
        self.core = FakeCore(settings={**SHARED, **(shared or {})}, module_settings=module_settings)
        self.order_calls: list[str] = []
        self._order = order
        self.tool_calls: list[dict] = []
        self.usage: list[dict] = []
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        settings.init(str(self.tmp))
        await self.core.start()
        manifest = await module_manifest.build()
        self.tools = [t for t in manifest["tools"] if t["name"] != "order"]
        self.client = ModuleClient(manifest, url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.link = CoreLink(self.client)
        rig = self

        async def order(text):
            rig.order_calls.append(text)
            return await rig._order(text) if rig._order else text.startswith("построй")
        module_manifest.attach(self.client, self.tools, order=order, graph=game_api.graph_data)

        @self.core.on_request("core.tool.call")
        def tool_call(body):
            rig.tool_calls.append(body)
            return {"text": "ok"}

        @self.core.on_request("core.claude_usage")
        def usage(body):
            rig.usage.append(body)
            return {}

        self._task = asyncio.create_task(self.link.run())
        await self.core.wait_connected()
        await self.client.wait_connected(5)
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()
        if self._task is not None and not self._task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
        await self.core.stop()
