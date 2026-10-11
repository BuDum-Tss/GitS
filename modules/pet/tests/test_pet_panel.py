"""Пространство «Питомец» в дашборде (TZ 15, TAB-9): блоки из настроек и присутствия, без кнопок; отправка ядру."""
import asyncio
import json

from manifest import build_manifest
from pet_logic import panel_blocks
from glados_module import ModuleClient
from glados_module.testing import FakeCore

import bridge

NOW = 1000.0


def test_panel_blocks_show_window_sticker_position_and_link():
    blocks = panel_blocks({"pet_visible": True, "pet_pinned": False, "pet_x": 10, "pet_y": 20},
                          {"emotion": "angry", "emotion_ts": NOW - 5}, NOW, True, True)
    assert [b["type"] for b in blocks] == ["stats", "text"]
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items == {"Окно": "показано", "Стикер": "angry", "Закреплён": "нет", "Позиция": "10, 20"}
    assert blocks[1]["text"] == "подключён к ядру"


def test_panel_blocks_when_hidden_disconnected_disabled_and_default_position():
    blocks = panel_blocks({"pet_visible": False, "pet_pinned": True, "pet_x": True, "pet_y": 5},
                          {"emotion": "happy", "emotion_ts": NOW}, NOW, False, False)
    items = {i["label"]: i["value"] for i in blocks[0]["items"]}
    assert items["Окно"] == "скрыто" and items["Закреплён"] == "да" and items["Позиция"] == "угол по умолчанию"
    assert items["Стикер"] == "resting"                                 # ядро недоступно
    assert blocks[1]["text"] == "не подключён к ядру; выключен"


def test_panel_has_no_actions():
    """Видимость меняет только человек (TG-13): в пространстве нет кнопок, манифест без actions."""
    blocks = panel_blocks({}, {}, NOW, True, True)
    assert "buttons" not in json.dumps(blocks) and build_manifest()["panel"] == {
        "title": "Питомец", "icon": "smile", "order": 80}


async def test_client_thread_sends_panel_to_core():
    async with FakeCore() as core:
        thread = bridge.ClientThread(build_manifest(), __import__("queue").Queue(), url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.05, 0.05, 0.1)
        thread.start()
        try:
            await asyncio.to_thread(thread.wait_started, 5)
            await core.wait_connected(timeout=5)
            await core.wait_for(lambda: core.panels.get("pet"), 5)
            assert core.panels["pet"]["blocks"][0]["type"] == "stats"
            assert core.violations == []
        finally:
            thread.request_stop()
            await asyncio.to_thread(thread.join, 5)
