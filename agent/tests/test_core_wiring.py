"""Слой модулей целиком (ТЗ 02): wire_modules + дашборд + настоящий процесс эхо-модуля, запущенный супервизором.
Повторяет ручную приёмку 3-4 (раздел 16): включение в дашборде, инструмент, задача с вопросом, выключение за <= 10 с."""
import asyncio
import socket
import time

import aiohttp
import psutil
from aiohttp import web
from hub_helpers import FakeCompanion

import approvals
import core_service
import dashboard
import settings
import speech_out
from environments.registry import Registry
from supervisor import load_specs

JSON = {"Content-Type": "application/json"}


class WiredCompanion(FakeCompanion):
    def __init__(self):
        super().__init__()
        self.presence = speech_out.PresenceTracker()
        self.io = None
        self.profile_hint = lambda: ""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for(predicate, timeout=15.0):
    async def waiter():
        while True:
            v = predicate()
            if v:
                return v
            await asyncio.sleep(0.05)
    return await asyncio.wait_for(waiter(), timeout)


async def test_echo_module_end_to_end_through_dashboard(tmp_path):
    port = free_port()
    companion, registry = WiredCompanion(), Registry()
    layer = core_service.wire_modules(companion, registry, port=port, battery=lambda: {"level": 1.0, "sleeping": False},
                                      specs=load_specs())
    dashboard._MEMORY_DIR = tmp_path / "memory"
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    dashboard.set_presence(companion.presence)
    dashboard.set_registry(registry)
    dashboard.set_modes(layer.profiles)
    runner = web.AppRunner(dashboard.build_app())
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    await layer.start()
    base = f"http://127.0.0.1:{port}"
    pid = None
    try:
        async with aiohttp.ClientSession() as http:
            async def get(path):
                async with http.get(base + path) as r:
                    return await r.json()

            async def post(path, body):
                async with http.post(base + path, json=body, headers=JSON) as r:
                    return r.status, await r.json()

            mods = {m["id"]: m for m in await get("/api/modules")}
            assert mods["echo"]["enabled"] is False and mods["echo"]["connected"] is False       # выключен по умолчанию
            assert mods["echo"]["status"] == "disabled" and mods["echo"]["configured"] is True
            assert layer.supervisor.state["echo"].task is None                                  # процесс не запущен

            assert (await post("/api/modules/echo/enabled", {"enabled": True}))[0] == 200
            await wait_for(lambda: layer.hub.is_connected("echo"))                              # запустил супервизор
            m = next(x for x in await get("/api/modules") if x["id"] == "echo")
            assert m["connected"] and m["status"] == "connected" and m["pid"] and m["version"] == "1.0.0"
            assert {t["id"] for t in m["tools"]} == {"echo.repeat", "echo.count"}
            pid = m["pid"]
            assert psutil.pid_exists(pid)

            # состояние для питомца: presence и краткий список модулей (CORE-39)
            state = await get("/api/state")
            assert state["presence"]["busy"] is False and {"id": "echo", "connected": True, "enabled": True} in state["modules"]

            # профиль assistant: echo.repeat виден (availability=always) и выполняется планировщиком;
            # задача с вопросом: прогресс во вкладке, ответ кнопкой доходит до модуля
            from router import Router
            router = Router(registry, lambda t: asyncio.sleep(0), lambda: "")
            layer.requests.router = router
            assert await router.run_tool("echo.repeat", {"text": "кукуруза"}) == (True, "кукуруза")
            ok, text = await router.run_tool("echo.count", {"n": 2})
            assert ok and text.startswith("задача j-")
            job_id = text.split()[1]
            job = await wait_for(lambda: next((j for j in layer.modules.jobs_all() if j["state"] == "waiting_user"), None))
            jobs = await get("/api/jobs")
            row = next(j for j in jobs if j["job_id"] == job_id)
            assert row["module"] == "echo" and row["question"]["kind"] == "choice" and row["progress"] is not None
            assert job["job_id"] == job_id
            status, body = await post(f"/api/jobs/echo/{job_id}/answer", {"question_id": "q-1", "answer": "не такой вариант"})
            assert status == 400
            status, body = await post(f"/api/jobs/echo/{job_id}/answer", {"question_id": "q-1", "answer": "продолжать"})
            assert status == 200
            await wait_for(lambda: next(j for j in layer.modules.jobs_all() if j["job_id"] == job_id)["state"] == "done", 20)
            assert not approvals.has_pending()                                                  # вопрос снят с очереди

            # выключение завершает процесс за <= 10 с
            t0 = time.monotonic()
            assert (await post("/api/modules/echo/enabled", {"enabled": False}))[0] == 200
            await wait_for(lambda: not psutil.pid_exists(pid), 12)
            assert time.monotonic() - t0 <= 10
            await wait_for(lambda: not layer.hub.is_connected("echo"))
            m = next(x for x in await get("/api/modules") if x["id"] == "echo")
            assert m["status"] == "disabled" and not m["connected"]

            # проверки эндпоинтов
            assert (await post("/api/modules/нет/enabled", {"enabled": True}))[0] == 404
            assert (await post("/api/modules/echo/enabled", {"enabled": "да"}))[0] == 400
            assert (await post("/api/modules/echo/settings", {"key": "x", "value": 1}))[0] == 400
    finally:
        await layer.stop()
        await runner.cleanup()
        if pid and psutil.pid_exists(pid):
            psutil.Process(pid).kill()
    assert settings.load()["modules"]["echo"]["enabled"] is False
