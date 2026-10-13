"""Сквозная проверка ТЗ 18 по настоящему WebSocket и HTTP: модуль с политиками подключается к ядру (MOD-1…MOD-14),
присылает событие критической срочности, получает `policy.activate` по сокету, а `/api/focus` показывает его в фокусе.
Модуль здесь - клиент SDK в этом же процессе, не настоящая игра. Claude и LM Studio не вызываются."""
import asyncio

import aiohttp
import dashboard
from test_dashboard_endpoints import serve

from gits_sdk import ModuleClient
from focus_rigs import GAME_POLICIES

MANIFEST = {
    "id": "rimworld",
    "title": "RimWorld",
    "description": "Игра-симулятор колонии (проверка менеджера фокуса).",
    "version": "1.0.0",
    "capabilities": ["game"],
    "subscribe": ["module"],
    "emits": [{"kind": "rimworld.raid", "description": "Начался набег на колонию", "default_urgency": 5}],
    "policies": GAME_POLICIES["policies"],
    "default_policy": GAME_POLICIES["default_policy"],
    "state": True,
}


async def test_critical_event_over_websocket_moves_module_into_focus(tmp_path):
    port, layer, runner = await serve(tmp_path)
    dashboard.set_focus(layer.focus)                     # как в core_service.run(): дашборд видит менеджер фокуса
    client = ModuleClient(MANIFEST, url=f"ws://127.0.0.1:{port}/module/ws", token=layer.hub.token)
    activations: list[dict] = []

    @client.on_request("policy.activate")
    async def activate(body):
        activations.append(body)
        return {"ok": True, "policy": body["policy"]}

    task = asyncio.create_task(client.run())
    try:
        await client.wait_connected(5)
        await client.state("в фоне", {"status": {"active_policy": "afk", "interruptible": True}})
        await client.event("rimworld.raid", "Набег: трое рейдеров у ворот", urgency_hint=5)

        async with aiohttp.ClientSession() as http:
            async def focus():
                async with http.get(f"http://127.0.0.1:{port}/api/focus") as r:
                    return await r.json()

            # ответ модуля доходит до ядра не сразу: ждём, пока фокус отразится в /api/focus
            body = None
            for _ in range(100):
                body = await focus()
                if body["holder"] is not None:
                    break
                await asyncio.sleep(0.1)
            assert activations and activations[-1]["policy"] == "observe"   # активная политика игры, не фоновая
            assert "task_id" in activations[-1] and activations[-1]["reason"]
            assert body["holder"] is not None and body["holder"]["module"] == "rimworld"
            assert body["holder"]["policy"] == "observe"
            assert any(r["type"] == "switch" and r["reason"] for r in body["journal"])
            assert body["policies"]["rimworld"]["default"] == "afk"
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        dashboard.set_focus(None)
        await layer.stop()
        await runner.cleanup()
