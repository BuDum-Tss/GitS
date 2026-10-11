"""Поток клиента модуля (ТЗ 13, PET-3): `ModuleClient.run()` в отдельном потоке со своим циклом asyncio.

tkinter живёт в главном потоке и сюда не заглядывает: всё, что узнал клиент (присутствие, настройки, связь с ядром,
включённость), кладётся в `queue.Queue` сообщениями `(kind, data)`; окно разбирает очередь через `root.after`.
"""
import asyncio
import logging
import queue
import threading

from glados_module import ModuleClient

log = logging.getLogger("pet")

WATCH_EVERY_S = 0.1            # как часто сверять связь с ядром, настройки и включённость


class ClientThread(threading.Thread):
    def __init__(self, manifest: dict, messages: "queue.Queue", *, url: str | None = None, token: str | None = None):
        super().__init__(name="pet-client", daemon=True)
        self.q = messages
        self.client = ModuleClient(manifest, url=url, token=token)
        self.exit_code: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._register()

    def _register(self) -> None:
        c = self.client

        @c.on_topic("presence")
        async def on_presence(data):
            self.q.put(("presence", dict(data)))

        @c.on_topic("settings")
        async def on_settings(data):
            self.q.put(("settings", dict(c.settings)))

        @c.on_topic("module")
        async def on_module(data):
            self.q.put(("enabled", bool(c.enabled)))

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

    async def _main(self) -> int:
        self.loop = asyncio.get_running_loop()
        watcher = self.loop.create_task(self._watch())
        self._started.set()
        try:
            return await self.client.run()
        finally:
            watcher.cancel()
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
