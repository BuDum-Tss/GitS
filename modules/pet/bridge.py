"""Поток клиента модуля (ТЗ 13, PET-3): `ModuleClient.run()` в отдельном потоке со своим циклом asyncio.

tkinter живёт в главном потоке и сюда не заглядывает: всё, что узнал клиент (присутствие, настройки, связь с ядром,
включённость), кладётся в `queue.Queue` сообщениями `(kind, data)`; окно разбирает очередь через `root.after`.
"""
import asyncio
import json
import logging
import queue
import threading
import time

from gits_sdk import ModuleClient, ToolError
from pet_logic import panel_blocks

log = logging.getLogger("pet")

WATCH_EVERY_S = 0.1            # как часто сверять связь с ядром, настройки и включённость
PANEL_EVERY_S = 2.0            # пространство «Питомец» в дашборде (TZ 15): проверка, отправка только при изменении


class ClientThread(threading.Thread):
    def __init__(self, manifest: dict, messages: "queue.Queue", *, url: str | None = None, token: str | None = None):
        super().__init__(name="pet-client", daemon=True)
        self.q = messages
        self.client = ModuleClient(manifest, url=url, token=token)
        self.exit_code: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self.last_presence: dict = {}                 # последнее presence для пространства в дашборде (TZ 15)
        self._panel_key: str | None = None
        self._register()

    def _register(self) -> None:
        c = self.client

        @c.on_topic("presence")
        async def on_presence(data):
            self.last_presence = dict(data)
            self.q.put(("presence", dict(data)))

        @c.on_topic("settings")
        async def on_settings(data):
            self.q.put(("settings", dict(c.settings)))

        @c.on_topic("module")
        async def on_module(data):
            self.q.put(("enabled", bool(c.enabled)))

        # кнопки пространства «Питомец» (TZ 15b): общая настройка pet_visible меняется через ядро (core.set_setting, TG-13)
        @c.panel_action("show")
        async def panel_show(args):
            return await self._set_visible(True)

        @c.panel_action("hide")
        async def panel_hide(args):
            return await self._set_visible(False)

    async def _set_visible(self, visible: bool) -> dict:
        """Показать или спрятать окно: инструмент ядра core.set_setting с ключом pet_visible. Ответ ядра - ok / сообщение."""
        try:
            await self.client.request("core.tool.call", {"tool": "core.set_setting",
                                                         "args": {"key": "pet_visible", "value": visible}}, timeout=10)
        except (ToolError, asyncio.TimeoutError) as e:
            return {"ok": False, "message": str(e) or "ядро не ответило"}
        return {"ok": True, "message": "питомец показан" if visible else "питомец скрыт"}

    async def _watch(self) -> None:
        """Связь, начальные настройки (welcome) и включённость не приходят темами: сверяем и сообщаем при изменении."""
        c = self.client
        sent_settings, connected, enabled = None, None, None
        while True:
            if c.connected != connected:
                connected = c.connected
                self.q.put(("connected", connected))
            if connected:
                if dict(c.settings) != sent_settings:
                    sent_settings = dict(c.settings)
                    self.q.put(("settings", dict(sent_settings)))
                if c.enabled != enabled:
                    enabled = c.enabled
                    self.q.put(("enabled", bool(enabled)))
            await asyncio.sleep(WATCH_EVERY_S)

    async def _panel_loop(self) -> None:
        """Пространство «Питомец» (TZ 15): окно, стикер, позиция и связь; шлётся только при изменении."""
        c = self.client
        while True:
            try:
                blocks = panel_blocks(dict(c.settings), self.last_presence, time.time(), c.connected, c.enabled)
                key = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                if key != self._panel_key and await c.panel.set(blocks):
                    self._panel_key = key
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("пространство питомца не обновлено: %s", e)
            await asyncio.sleep(PANEL_EVERY_S)

    async def _main(self) -> int:
        self.loop = asyncio.get_running_loop()
        watcher = self.loop.create_task(self._watch())
        panel = self.loop.create_task(self._panel_loop())
        self._started.set()
        try:
            return await self.client.run()
        finally:
            watcher.cancel()
            panel.cancel()
            self.q.put(("connected", False))

    def run(self) -> None:
        try:
            self.exit_code = asyncio.run(self._main())
        except Exception:
            log.exception("поток клиента модуля завершился с ошибкой")
            self.exit_code = 1

    def request_stop(self) -> None:
        """Закрыть соединение и завершить поток клиента (из любого потока)."""
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self.client.stop(), loop)
            except RuntimeError:
                pass

    def wait_started(self, timeout: float = 5.0) -> bool:
        return self._started.wait(timeout)
