"""Мостик MCP-сервера игры к ядру через API модуля (ТЗ 11, RW-16): управление экраном и поиск в интернете."""
import socket

import aiohttp
import pytest
from gits_sdk import ToolError

import core_bridge
import game_api

JSON = {"Content-Type": "application/json"}


class FakeCoreClient:
    def __init__(self):
        self.requests = []
        self.reply = {"text": "готово"}

    async def request(self, msg_type, body, timeout=60.0):
        self.requests.append((msg_type, body))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def api(tmp_path, monkeypatch):
    core = FakeCoreClient()
    port = free_port()
    monkeypatch.setenv("RIMWORLD_MODULE_PORT", str(port))
    runner = await game_api.start(str(tmp_path), port=port, core=core)
    yield core, f"http://127.0.0.1:{port}"
    await runner.cleanup()
    game_api._CORE = None


async def post(base, path, body, headers=JSON):
    async with aiohttp.ClientSession() as http:
        async with http.post(base + path, json=body, headers=headers) as r:
            return r.status, await r.json()


async def test_core_tool_forwards_allowed_screen_tools(api):
    core, base = api
    status, body = await post(base, "/api/core/tool", {"tool": "system.click_text", "args": {"text": "Играть"}})
    assert status == 200 and body == {"ok": True, "result": "готово"}
    assert core.requests == [("core.tool.call", {"tool": "system.click_text", "args": {"text": "Играть"}})]


async def test_core_tool_refuses_other_tools_and_non_json(api):
    core, base = api
    status, body = await post(base, "/api/core/tool", {"tool": "telegram.send", "args": {}})
    assert status == 403 and body["ok"] is False and core.requests == []
    async with aiohttp.ClientSession() as http:
        async with http.post(base + "/api/core/tool", data="{}") as r:
            assert r.status == 403                                                     # не JSON: как остальные эндпоинты


async def test_core_tool_reports_core_refusal_as_not_ok(api):
    core, base = api
    core.reply = ToolError("пользователь не разрешил", code="denied")
    status, body = await post(base, "/api/core/tool", {"tool": "system.press_key", "args": {"keys": "esc"}})
    assert status == 200 and body == {"ok": False, "result": "пользователь не разрешил"}


async def test_web_search_goes_to_core(api):
    core, base = api
    core.reply = {"text": "потолок держится на расстоянии 6 клеток"}
    status, body = await post(base, "/api/core/web_search", {"query": "RimWorld roof support distance"})
    assert status == 200 and body["answer"].startswith("потолок")
    assert core.requests == [("core.web_search", {"query": "RimWorld roof support distance"})]


async def test_bridge_helpers_call_the_module_api(api):
    import asyncio
    core, base = api
    assert await asyncio.to_thread(core_bridge.tool, "system.read_screen", {}) == "готово"       # так зовёт MCP-сервер из потока
    core.reply = {"text": "ответ"}
    assert await asyncio.to_thread(core_bridge.web_search, "запрос") == "ответ"


async def test_bridge_helpers_report_errors_as_strings(monkeypatch):
    monkeypatch.setenv("RIMWORLD_MODULE_PORT", str(free_port()))                             # API не поднято
    assert core_bridge.tool("system.read_screen", {}).startswith("ERROR: ядро недоступно")
    assert core_bridge.web_search("x").startswith("ERROR: поиск недоступен")


def test_api_port_prefers_new_variable(monkeypatch):
    monkeypatch.delenv("RIMWORLD_MODULE_PORT", raising=False)
    monkeypatch.delenv("RIMWORLD_AGENT_PORT", raising=False)
    assert core_bridge.api_port() == 8767
    monkeypatch.setenv("RIMWORLD_AGENT_PORT", "9001")
    assert core_bridge.api_port() == 9001                                                    # прежнее имя: запасное
    monkeypatch.setenv("RIMWORLD_MODULE_PORT", "9002")
    assert core_bridge.api_port() == 9002


def test_mcp_functions_use_the_bridge_not_the_old_assistant_url(monkeypatch):
    import rimworld_mcp
    calls = []
    monkeypatch.setattr(core_bridge, "tool", lambda tool_id, args: calls.append((tool_id, args)) or "ok")
    monkeypatch.setattr(core_bridge, "web_search", lambda q: calls.append(("search", q)) or "найдено")
    assert rimworld_mcp.pc_press_key("esc") == "ok" and rimworld_mcp.web_search("roof") == "найдено"
    assert calls == [("system.press_key", {"keys": "esc"}), ("search", "roof")]
    assert not hasattr(rimworld_mcp, "_ASSISTANT_URL")
