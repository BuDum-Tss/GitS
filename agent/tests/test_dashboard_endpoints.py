"""Эндпоинты дашборда для модулей, профилей и вопросов (ТЗ 02, CORE-22, CORE-30, CORE-37, CORE-42) без процессов модулей."""
import asyncio

import aiohttp
from aiohttp import web
from test_core_wiring import JSON, WiredCompanion, free_port, wait_for

import approvals
import core_service
import dashboard
import settings
from environments.registry import Registry


async def serve(tmp_path, specs=()):
    port = free_port()
    companion, registry = WiredCompanion(), Registry()
    layer = core_service.wire_modules(companion, registry, port=port, battery=lambda: {"level": 1.0, "sleeping": False},
                                      specs=list(specs))
    dashboard._MEMORY_DIR = tmp_path / "memory"
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    dashboard.set_presence(companion.presence)
    dashboard.set_registry(registry)
    dashboard.set_modes(layer.profiles)
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    await layer.start()
    return port, layer, runner


async def test_dashboard_module_settings_mode_approval_and_job_endpoints(tmp_path):
    from glados_module import ModuleClient
    port, layer, runner = await serve(tmp_path)
    manifest = {"id": "cfg", "title": "Настройки", "description": "Проверка настроек.", "version": "1.0.0",
                "subscribe": ["settings"], "tools": [],
                "settings_schema": {"poll": {"type": "integer", "title": "Опрос", "default": 5, "min": 1, "max": 60},
                                    "mode": {"type": "string", "title": "Режим", "default": "a", "enum": ["a", "b"]},
                                    "loud": {"type": "boolean", "title": "Громко", "default": False}}}
    client = ModuleClient(manifest, url=f"ws://127.0.0.1:{port}/module/ws", token=layer.hub.token)
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
            body = await post("/api/modules/cfg/settings", {"key": "poll", "value": 20})
            assert body["module_settings"] == {"poll": 20, "mode": "a", "loud": False}
            await wait_for(lambda: client.module_settings.get("poll") == 20)                    # тема settings дошла до модуля
            await post("/api/modules/cfg/settings", {"key": "poll", "value": 0}, 400)             # меньше min
            await post("/api/modules/cfg/settings", {"key": "poll", "value": "5"}, 400)           # не тот тип
            await post("/api/modules/cfg/settings", {"key": "poll", "value": True}, 400)          # bool не integer
            await post("/api/modules/cfg/settings", {"key": "mode", "value": "c"}, 400)           # не из enum
            await post("/api/modules/cfg/settings", {"key": "nope", "value": 1}, 400)
            await post("/api/modules/cfg/settings", {"key": "loud", "value": True})
            assert settings.load()["modules"]["cfg"] == {"poll": 20, "loud": True}                # слияние: ключи не затёрты
            mods = {m["id"]: m for m in await get("/api/modules")}
            assert mods["cfg"]["module_settings"] == {"poll": 20, "mode": "a", "loud": True}
            assert mods["cfg"]["settings_schema"]["poll"]["max"] == 60

            # перезапустить можно только то, что запускает ядро; задачи офлайн-модуля не отменить
            await post("/api/modules/cfg/restart", {}, 400)
            await post("/api/jobs/нет/j-00000000/cancel", {}, 409)
            await post("/api/jobs/cfg/j-00000000/answer", {"question_id": "q", "answer": True}, 400)
            assert await get("/api/jobs") == []

            # профили: ручной выбор, неготовый профиль отклоняется, auto возвращает автоматику (CORE-22)
            g = await get("/api/modes")
            assert g["current"] == "assistant" and g["override"] is None
            await post("/api/mode", {"mode": "work"})
            g = await get("/api/modes")
            assert g["current"] == "work" and g["override"] == "work" and settings.load()["profile"] == "work"
            await post("/api/mode", {"mode": "minecraft"}, 400)
            await post("/api/mode", {"mode": "auto"})
            assert (await get("/api/modes"))["current"] == "assistant"

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
            async with http.post(base + "/api/modules/cfg/restart", data="{}") as r:
                assert r.status == 403
    finally:
        await client.stop()
        await asyncio.wait_for(task, 5)
        await layer.stop()
        await runner.cleanup()


async def test_legacy_http_channels_still_registered():
    app = dashboard.build_app()
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    for r in (("POST", "/api/say"), ("GET", "/api/speech_status"), ("POST", "/api/emotion"), ("POST", "/api/event"),
              ("POST", "/api/tool"), ("POST", "/api/web_search"), ("POST", "/api/claude_usage"), ("GET", "/api/modes"),
              ("POST", "/api/mode"), ("GET", "/api/state"), ("POST", "/api/chat"), ("POST", "/api/approval")):
        assert r in routes, r
    assert ("GET", "/api/modules") in routes and ("POST", "/api/modules/{id}/enabled") in routes
    assert ("POST", "/api/jobs/{module}/{job_id}/answer") in routes


async def test_legacy_say_goes_through_attention_as_verbatim(tmp_path):
    """CORE-42: /api/say агента игры вызывает attention.submit_speech('rimworld', verbatim)."""
    companion, registry = WiredCompanion(), Registry()
    layer = core_service.wire_modules(companion, registry, specs=[])
    calls = []

    async def spy(module_id, say):
        calls.append((module_id, say))
    layer.attention.submit_speech = spy

    async def link_say(text, emotion=None):          # та же связка, что в core_service._run
        await layer.attention.submit_speech("rimworld", {"text": text, "mode": "verbatim", "emotion": emotion})
    dashboard.set_game_link(say=link_say)
    dashboard._MEMORY_DIR = tmp_path / "memory"
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    port = free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        async with aiohttp.ClientSession() as http:
            url = f"http://127.0.0.1:{port}/api/say"
            async with http.post(url, json={"text": "Борис уснул", "emotion": "angry"}, headers=JSON) as r:
                assert r.status == 200
    finally:
        await runner.cleanup()
    assert calls == [("rimworld", {"text": "Борис уснул", "mode": "verbatim", "emotion": "angry"})]
