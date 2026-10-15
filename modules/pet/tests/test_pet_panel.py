"""Пространство «Питомец» в дашборде (TZ 15, TAB-9; TZ 15b: кнопки «Показать» и «Скрыть»): блоки, действия, отправка ядру."""
import asyncio
import json
import queue

from manifest import build_manifest
from pet_logic import panel_blocks
from gits_sdk import ToolError
from gits_sdk.testing import FakeCore

import bridge

NOW = 1000.0
BUTTONS = {"type": "buttons", "items": [{"label": "Показать", "action": "show"}, {"label": "Скрыть", "action": "hide"}]}


def test_panel_blocks_show_window_sticker_position_and_link():
    blocks = panel_blocks({"pet_visible": True, "pet_pinned": False, "pet_x": 10, "pet_y": 20},
                          {"emotion": "angry", "emotion_ts": NOW - 5}, NOW, True, True)
    assert [b["type"] for b in blocks] == ["stats", "text", "buttons"]
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


def test_panel_shows_state_and_both_buttons_whatever_the_state():
    """Видимость видна в «Окно»; кнопки «Показать» (show) и «Скрыть» (hide) есть и когда окно показано, и когда скрыто."""
    for visible in (True, False):
        blocks = panel_blocks({"pet_visible": visible}, {}, NOW, True, True)
        assert blocks[-1] == BUTTONS
        assert {i["label"]: i["value"] for i in blocks[0]["items"]}["Окно"] == ("показано" if visible else "скрыто")


def test_manifest_declares_show_and_hide_actions_only():
    panel = build_manifest()["panel"]
    assert panel == {"title": "Питомец", "icon": "smile", "order": 80, "actions": ["show", "hide"]}


async def test_show_and_hide_buttons_change_pet_visible_through_core_tool_call():
    """TZ 15b: нажатие «Показать»/«Скрыть» уходит ядру как core.tool.call core.set_setting с pet_visible."""
    calls = []
    async with FakeCore() as core:
        @core.on_request("core.tool.call")
        def tool_call(body):
            calls.append(body)
            return {"text": "настройка pet_visible изменена"}

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
            assert [c["tool"] for c in calls] == ["core.set_setting", "core.set_setting"]
            assert calls[0]["args"] == {"key": "pet_visible", "value": True}
            assert calls[1]["args"] == {"key": "pet_visible", "value": False}
            assert core.violations == []
        finally:
            thread.request_stop()
            await asyncio.to_thread(thread.join, 5)


async def test_refused_or_failed_core_call_answers_not_ok_with_reason():
    async with FakeCore() as core:
        @core.on_request("core.tool.call")
        def tool_call(body):
            raise ToolError("настройку «pet_visible» менять нельзя", code="denied")

        thread = bridge.ClientThread(build_manifest(), queue.Queue(), url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.05, 0.05, 0.1)
        thread.start()
        try:
            await asyncio.to_thread(thread.wait_started, 5)
            await core.wait_connected(timeout=5)
            answer = await core.panel_action("hide")
            assert answer["ok"] is False and "pet_visible" in answer["message"]
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
