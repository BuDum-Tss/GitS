"""Типизированный ответ инструмента `{text, delivered_to}` (контракт 3.3): SDK, реестр модулей ядра, роутер."""
import pytest

from environments.base import Tool, ToolOutput
from gits_sdk import ToolReply, contract
from gits_sdk.protocol import check_delivered_to
from services.peers.registry import PeerRegistry
from services.tools.router import Router


def test_tool_reply_wire_and_validation():
    assert ToolReply("ok", ("channel:1", "user:2#6")).to_wire() == {"text": "ok", "delivered_to": ["channel:1", "user:2#6"]}
    assert ToolReply("ok").to_wire() == {"text": "ok"}                       # без адресов форма прежняя
    for bad in (("a",) * 9, ("a" * 201,), ("",), (5,)):
        with pytest.raises(ValueError):
            ToolReply("x", bad)
    assert check_delivered_to("channel:1") and not check_delivered_to(["channel:1"])


def test_contract_accepts_typed_and_plain_tool_results():
    for ok in ("текст", {"job_id": "j"}, {"text": "x"}, {"text": "x", "delivered_to": ["channel:1"]}):
        assert contract.validate_response("tool.call", ok) == [], ok
    for bad in ({"text": "x", "delivered_to": "channel:1"}, {"text": "x", "delivered_to": ["a"] * 9},
                {"text": "x", "delivered_to": ["a" * 201]}):
        assert contract.validate_response("tool.call", bad), bad


def test_registry_extracts_delivered_to_and_drops_bad_lists():
    f = PeerRegistry.delivered_to
    assert f({"text": "ok", "delivered_to": ["channel:7"]}) == ("channel:7",)
    assert f("строка") is None and f({"job_id": "j"}) is None and f({"text": "x"}) is None
    assert f({"text": "ok", "delivered_to": ["a"] * 9}) == ()


async def test_router_outcome_carries_delivered_to_and_wrappers_keep_signatures():
    async def typed(args):
        return ToolOutput("отправлено", ("channel:7",))

    async def plain(args):
        return "просто"
    reg = type("Reg", (), {"get": lambda self, i: {"a.message": Tool("a.message", "a", "message", "d", risk="read", call=typed),
                                                    "a.plain": Tool("a.plain", "a", "plain", "d", risk="read", call=plain)}.get(i)})()
    r = Router(reg, None)
    out = await r.run_tool_outcome("a.message", {})
    assert (out.ok, out.text, out.code, out.delivered_to) == (True, "отправлено", None, ("channel:7",))
    assert await r.run_tool_ex("a.message", {}) == (True, "отправлено", None)
    assert await r.run_tool("a.plain", {}) == (True, "просто")
    assert (await r.run_tool_outcome("a.plain", {})).delivered_to == ()
    assert (await r.run_tool_outcome("a.none", {})).code == "unknown"
