"""Эндпоинты дашборда для модулей, профилей и вопросов (ТЗ 02, CORE-22, CORE-30, CORE-37, CORE-42) без процессов модулей."""
import asyncio

import aiohttp
from aiohttp import web
from test_core_wiring import JSON, WiredCompanion, free_port, wait_for

from services.conversation import approvals
import core_service
import http_api
from host import settings
from environments.registry import Registry


async def serve(tmp_path, specs=()):
    port = free_port()
    companion, registry = WiredCompanion(), Registry()
    layer = core_service.wire_modules(companion, registry, port=port,
                                      specs=list(specs))
    http_api._MEMORY_DIR = tmp_path / "memory"
    http_api.set_modules(layer.modules, layer.hub, layer.supervisor)
    http_api.set_presence(companion.presence)
    http_api.set_registry(registry)
    http_api.set_focus(layer.focus)
    runner = web.AppRunner(http_api.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    await layer.start()
    return port, layer, runner


async def test_dashboard_module_settings_mode_approval_and_job_endpoints(tmp_path):
    from gits_sdk import WorkspaceClient
    port, layer, runner = await serve(tmp_path)
    manifest = {"id": "cfg", "role": "workspace", "ownership": "owned", "title": "Настройки", "description": "Проверка настроек.", "version": "1.0.0",
                "subscribe": ["settings"], "tools": [],
                "settings_schema": {"poll": {"type": "integer", "title": "Опрос", "default": 5, "min": 1, "max": 60},
                                    "mode": {"type": "string", "title": "Режим", "default": "a", "enum": ["a", "b"]},
                                    "loud": {"type": "boolean", "title": "Громко", "default": False}}}
    client = WorkspaceClient(manifest, url=f"ws://127.0.0.1:{port}/ws", token=layer.hub.token)
    task = asyncio.create_task(client.run())
    base = f"http://127.0.0.1:{port}"
    try:
        await client.wait_connected(5)
        async with aiohttp.ClientSession() as http:
            async def post(path, body, status=200):
                async with http.post(base + path, json=body, headers=JSON) as r:
                    assert r.status == status, (path, body, r.status, await r.text())
                    return await r.json()

            async def get(path):
                async with http.get(base + path) as r:
                    return await r.json()

            # собственные настройки по схеме (CORE-37)
            body = await post("/api/peers/cfg/settings", {"key": "poll", "value": 20})
            assert body["module_settings"] == {"poll": 20, "mode": "a", "loud": False}
            await wait_for(lambda: client.module_settings.get("poll") == 20)                    # тема settings дошла до модуля
            await post("/api/peers/cfg/settings", {"key": "poll", "value": 0}, 400)             # меньше min
            await post("/api/peers/cfg/settings", {"key": "poll", "value": "5"}, 400)           # не тот тип
            await post("/api/peers/cfg/settings", {"key": "poll", "value": True}, 400)          # bool не integer
            await post("/api/peers/cfg/settings", {"key": "mode", "value": "c"}, 400)           # не из enum
            await post("/api/peers/cfg/settings", {"key": "nope", "value": 1}, 400)
            await post("/api/peers/cfg/settings", {"key": "loud", "value": True})
            assert settings.load()["peers"]["cfg"] == {"poll": 20, "loud": True}                # слияние: ключи не затёрты
            mods = {m["id"]: m for m in await get("/api/workspaces")}
            assert mods["cfg"]["module_settings"] == {"poll": 20, "mode": "a", "loud": True}
            assert mods["cfg"]["settings_schema"]["poll"]["max"] == 60

            # перезапустить можно только то, что запускает ядро; задачи офлайн-модуля не отменить
            await post("/api/peers/cfg/restart", {}, 400)
            await post("/api/jobs/нет/j-00000000/cancel", {}, 409)
            await post("/api/jobs/cfg/j-00000000/answer", {"question_id": "q", "answer": True}, 400)
            assert await get("/api/jobs") == []

            # фокус среды: текущая среда и видимые модули
            g = await get("/api/space")
            assert g["current"] == "assistant" and "desktop" in g["focus_modules"] and "streamer" in g["flags"]

            # вопросы: кнопки choice и text в /api/approval (CORE-30)
            ask = asyncio.create_task(approvals.ask("choice", "Какой?", ["красный", "синий"], source="cfg"))
            await wait_for(approvals.has_pending)
            core = await get("/api/core")
            assert core["approval"]["pending"] and core["approval"]["kind"] == "choice" and core["approval"]["options"] == ["красный", "синий"]
            assert (await post("/api/approval", {"answer": "зелёный"}))["resolved"] is False
            assert (await post("/api/approval", {"answer": "синий"}))["resolved"] is True
            assert await ask == ("синий", "user")
            ask = asyncio.create_task(approvals.ask("text", "Имя?"))
            await wait_for(approvals.has_pending)
            assert (await post("/api/approval", {"answer": "Куб"}))["resolved"] is True
            assert await ask == ("Куб", "user")
            ask = asyncio.create_task(approvals.ask("approval", "Можно?"))
            await wait_for(approvals.has_pending)
            await post("/api/approval", {"allow": False})
            assert await ask == (False, "user")

            # POST без JSON-заголовка запрещён (как остальные эндпоинты)
            async with http.post(base + "/api/peers/cfg/restart", data="{}") as r:
                assert r.status == 403
    finally:
        await client.stop()
        await asyncio.wait_for(task, 5)
        await layer.stop()
        await runner.cleanup()


async def test_core_dashboard_routes_remain_after_old_game_channels_were_removed():
    app = http_api.build_app()
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    for r in (("GET", "/api/space"), ("GET", "/api/state"), ("POST", "/api/chat"),
              ("POST", "/api/approval"), ("POST", "/api/appraisal_label")):
        assert r in routes, r
    assert ("GET", "/api/workspaces") in routes and ("GET", "/api/services") in routes and ("GET", "/api/modules") not in routes and ("POST", "/api/peers/{id}/enabled") in routes
    assert ("POST", "/api/jobs/{module}/{job_id}/answer") in routes
    # ТЗ 11, RW-18: каналы старого агента игры удалены (игра теперь модуль на шине)
    for path in ("/api/say", "/api/speech_status", "/api/emotion", "/api/event", "/api/tool", "/api/web_search", "/api/claude_usage"):
        assert not [m for m, p in routes if p == path], path
