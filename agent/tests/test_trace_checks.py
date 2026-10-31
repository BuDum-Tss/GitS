"""Проверки ошибок в разговоре и маршрутах (ТЗ 24, TRC-5): поправка по шаблону и провал инструмента."""
from types import SimpleNamespace

import events_log
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


async def test_clear_correction_is_journaled_without_a_model(mk):
    rig = mk()
    rig.companion._last_say_text = "прошлая реплика"
    rig.companion._check_correction("нет, не так")
    rows = _mistakes("correction")
    assert rows and rows[0]["data"]["previous"] == "прошлая реплика"


async def test_unclear_start_is_not_journaled_as_correction(mk):
    rig = mk()
    rig.companion._check_correction("нет, мне нравится")
    assert _mistakes("correction") == []


async def test_failed_tool_step_writes_tool_failed_mistake():
    async def boom(args):
        raise ToolError("устройство не отвечает", code="unavailable")

    tool = SimpleNamespace(id="home.light", name="light", params={}, danger=None, danger_check=False, check=None,
                           risk="auto", self_confirming=False, timeout_s=5, informative=False, description="",
                           call=boom)
    router = Router(SimpleNamespace(), lambda *a, **k: None)
    ok, _name, info, code = await router._step({"tool": "home.light", "args": {}}, {"home.light": tool})
    assert not ok and code == "unavailable"
    rows = _mistakes("tool_failed")
    assert rows and rows[0]["data"]["code"] == "unavailable"
