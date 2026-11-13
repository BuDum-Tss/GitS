"""Пространство «Питомец» в дашборде (TZ 15, TAB-9; TZ 15b: кнопки «Показать» и «Скрыть»): блоки, действия, отправка ядру."""
import asyncio
import json
import queue

from manifest import build_manifest
from pet_logic import panel_blocks
from aiohttp import web
from gits_sdk.testing import FakeCore

import bridge

NOW = 1000.0
BUTTONS = {"type": "buttons", "items": [{"label": "Показать", "action": "show"}, {"label": "Скрыть", "action": "hide"}]}


def test_panel_blocks_show_window_sticker_position_and_link():
    blocks = panel_blocks({"pet_visible": True, "pet_pinned": False, "pet_x": 10, "pet_y": 20},
                          {"sticker": "angry", "sticker_ts": NOW - 5}, NOW, True, True)
    assert [b["type"] for b in blocks] == ["stats", "text", "buttons"]
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items == {"Окно": "показано", "Стикер": "angry", "Закреплён": "нет", "Позиция": "10, 20"}
    assert blocks[1]["text"] == "подключён к ядру"


def test_panel_blocks_when_hidden_disconnected_disabled_and_default_position():
    blocks = panel_blocks({"pet_visible": False, "pet_pinned": True, "pet_x": True, "pet_y": 5},
                          {"sticker": "happy", "sticker_ts": NOW}, NOW, False, False)
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items["Окно"] == "скрыто" and items["Закреплён"] == "да" and items["Позиция"] == "угол по умолчанию"
    assert items["Стикер"] == "resting"                                 # ядро недоступно
    assert blocks[1]["text"] == "не подключён к ядру; выключен"


def test_panel_shows_state_and_both_buttons_whatever_the_state():
    """Видимость видна в «Окно»; кнопки «Показать» (show) и «Скрыть» (hide) есть и когда окно показано, и когда скрыто."""
    for visible in (True, False):
        blocks = panel_blocks({"pet_visible": visible}, {}, NOW, True, True)
        assert blocks[-1] == BUTTONS
        assert {i["label"]: i["value"] for i in blocks[0]["items"]}["Окно"] == ("показано" if visible else "скрыто")


def test_manifest_declares_show_and_hide_actions_only():
    panel = build_manifest()["panel"]
    assert panel == {"title": "Питомец", "icon": "smile", "order": 80, "actions": ["show", "hide"]}


class FakeDashboard:
    """Дашборд ядра в одном запросе: `POST /api/peers/pet/settings` (собственные настройки питомца). Записывает тела запросов."""

    def __init__(self, status: int = 200, error: str = ""):
        self.calls: list[dict] = []
        self.status, self.error = status, error

    async def __aenter__(self):
        async def handler(request):
            self.calls.append(await request.json())
            if self.status >= 400:
                return web.json_response({"error": self.error}, status=self.status)
            return web.json_response({"ok": True})
        app = web.Application()
        app.router.add_post("/api/peers/pet/settings", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


async def test_show_and_hide_buttons_change_own_visible_setting_through_the_dashboard(monkeypatch):
    """TZ 15b: нажатие «Показать»/«Скрыть» меняет собственную настройку питомца visible через дашборд (`peers.pet`), общих настроек
    ядра и инструментов ядра не трогает."""
    async with FakeDashboard() as dash, FakeCore() as core:
        monkeypatch.setenv("GITS_DASHBOARD_URL", dash.url)
        thread = bridge.ClientThread(build_manifest(), queue.Queue(), url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.05, 0.05, 0.1)
        thread.start()
        try:
            await asyncio.to_thread(thread.wait_started, 5)
            await core.wait_connected(timeout=5)
            shown = await core.panel_action("show")
            hidden = await core.panel_action("hide")
            assert shown == {"ok": True, "message": "питомец показан"}
            assert hidden == {"ok": True, "message": "питомец скрыт"}
            assert dash.calls == [{"key": "visible", "value": True}, {"key": "visible", "value": False}]
            assert core.violations == []
        finally:
            thread.request_stop()
            await asyncio.to_thread(thread.join, 5)


async def test_refused_dashboard_answers_not_ok_with_reason(monkeypatch):
    async with FakeDashboard(status=400, error="у модуля нет настройки «visible»") as dash, FakeCore() as core:
        monkeypatch.setenv("GITS_DASHBOARD_URL", dash.url)
        thread = bridge.ClientThread(build_manifest(), queue.Queue(), url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.05, 0.05, 0.1)
        thread.start()
        try:
            await asyncio.to_thread(thread.wait_started, 5)
            await core.wait_connected(timeout=5)
            answer = await core.panel_action("hide")
            assert answer["ok"] is False and "visible" in answer["message"]
        finally:
            thread.request_stop()
            await asyncio.to_thread(thread.join, 5)


async def test_client_thread_sends_panel_to_core():
    async with FakeCore() as core:
        thread = bridge.ClientThread(build_manifest(), queue.Queue(), url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.05, 0.05, 0.1)
        thread.start()
        try:
            await asyncio.to_thread(thread.wait_started, 5)
            await core.wait_connected(timeout=5)
            await core.wait_for(lambda: core.panels.get("pet"), 5)
            assert core.panels["pet"]["blocks"][0]["type"] == "stats"
            assert json.dumps(core.panels["pet"]["blocks"][-1], ensure_ascii=False) == json.dumps(BUTTONS, ensure_ascii=False)
            assert core.violations == []
        finally:
            thread.request_stop()
            await asyncio.to_thread(thread.join, 5)
