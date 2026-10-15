"""Проверки ошибок в разговоре и маршрутах (ТЗ 24, TRC-5): поправка, которую проверяет модель в фоне, и провал инструмента."""
from types import SimpleNamespace

import events_log
import fast_brain
import pytest
from test_companion_attention import Rig

from environments.base import ToolError
from router import Router


@pytest.fixture
def mk(tmp_path, monkeypatch):
    return lambda **kw: Rig(tmp_path, monkeypatch, **kw)


def _mistakes(kind):
    events_log.flush()
    rows = events_log.tail(limit=500, kinds={"mistake"})
    return [r for r in rows if (r.get("data") or {}).get("type") == kind]


async def test_unclear_correction_is_checked_by_model_in_background(mk, monkeypatch):
    rig = mk()
    calls = []

    async def ask(system, user, **kwargs):
        calls.append(kwargs.get("background"))
        return '{"correction": true}'
    monkeypatch.setattr(fast_brain, "_ask", ask)
    await rig.companion._confirm_correction("нет", 1.0, "прошлая реплика")
    assert calls == [True]                       # фоновый вызов: уступает основному разговору
    rows = _mistakes("correction")
    assert rows and rows[0]["data"]["checked"] == "llm"


async def test_model_saying_no_correction_writes_nothing(mk, monkeypatch):
    rig = mk()

    async def ask(system, user, **kwargs):
        return '{"correction": false}'
    monkeypatch.setattr(fast_brain, "_ask", ask)
    await rig.companion._confirm_correction("нет, мне нравится", 1.0, "прошлая")
    assert _mistakes("correction") == []


async def test_failed_tool_step_writes_tool_failed_mistake():
    async def boom(args):
        raise ToolError("устройство не отвечает", code="unavailable")

    tool = SimpleNamespace(id="home.light", name="light", params={}, danger=None, danger_check=False, check=None,
                           risk="auto", self_confirming=False, timeout_s=5, informative=False, description="",
                           call=boom)
    router = Router(SimpleNamespace(), lambda *a, **k: None, lambda: "")
    ok, _name, info, code = await router._step({"tool": "home.light", "args": {}}, {"home.light": tool})
    assert not ok and code == "unavailable"
    rows = _mistakes("tool_failed")
    assert rows and rows[0]["data"]["code"] == "unavailable"
