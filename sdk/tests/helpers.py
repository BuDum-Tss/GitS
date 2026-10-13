"""Общее для тестов клиента: манифест-пример, запуск клиента против FakeCore."""
import asyncio
import contextlib

from gits_sdk import ModuleClient, ToolError

MANIFEST = {
    "id": "demo",
    "title": "Демо",
    "description": "Тестовый модуль для проверки SDK.",
    "version": "1.0.0",
    "capabilities": [],
    "subscribe": ["presence", "settings", "profile", "battery"],
    "shared_settings": ["streamer", "dnd"],
    "settings_schema": {"poll_seconds": {"type": "integer", "title": "Опрос", "default": 5, "min": 1, "max": 60}},
    "tools": [
        {"name": "ping", "description": "Ответить pong.", "params": {"text": {"type": "string"}},
         "risk": "auto", "availability": "always"},
        {"name": "slow", "description": "Долго ждать.", "params": {}, "risk": "auto", "availability": "always"},
        {"name": "refuse", "description": "Всегда отказывает.", "params": {}, "risk": "auto",
         "availability": "always"},
        {"name": "boom", "description": "Падает исключением.", "params": {}, "risk": "auto",
         "availability": "always"},
        {"name": "huge", "description": "Отдаёт слишком много.", "params": {}, "risk": "auto",
         "availability": "always"},
        {"name": "weird", "description": "Отдаёт не JSON.", "params": {}, "risk": "auto", "availability": "always"},
        {"name": "wipe", "description": "Удалить файлы.", "params": {"path": {"type": "string"}}, "risk": "ask",
         "availability": "profile", "danger_check": True},
    ],
    "emits": [{"kind": "demo.thing", "description": "Что-то случилось", "default_urgency": 2}],
    "state": True,
}


def make_client(core, *, handlers=True, manifest=None, token="core") -> ModuleClient:
    """Клиент к FakeCore с быстрыми переподключениями и стандартными обработчиками.
    token="core": токен FakeCore; None: клиент ищет токен сам (переменная окружения, файл)."""
    client = ModuleClient(manifest or MANIFEST, url=core.url, token=core.token if token == "core" else token)
    client.reconnect_delays = (0.05, 0.05, 0.1)
    if not handlers:
        return client
    client.gate = asyncio.Event()           # для tool slow

    @client.tool("ping")
    async def ping(args):
        return {"pong": args.get("text", "")}

    @client.tool("slow")
    async def slow(args):
        await client.gate.wait()
        return "готово"

    @client.tool("refuse")
    async def refuse(args):
        raise ToolError("пользователь не разрешил", code="denied")

    @client.tool("boom")
    async def boom(args):
        raise ValueError("сломалось внутри")

    @client.tool("huge")
    async def huge(args):
        return "я" * (2 * 1024 * 1024)

    @client.tool("weird")
    async def weird(args):
        return object()

    @client.tool("wipe")
    async def wipe(args):
        return "удалено"

    @client.check("wipe")
    async def wipe_check(args):
        return "это удалит файлы" if "важное" in args.get("path", "") else None

    return client


@contextlib.asynccontextmanager
async def running(client, core, *, connected=True):
    """Запустить `client.run()` фоновой задачей, дождаться рукопожатия, в конце остановить."""
    task = asyncio.create_task(client.run())
    try:
        if connected:
            await core.wait_connected()
            await client.wait_connected(5)
        yield task
    finally:
        await client.stop()
        if not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
