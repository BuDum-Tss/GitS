"""Пространство модуля в клиенте против FakeCore (TZ 15, TAB-1 и TAB-8): panel.set, panel.action, отказоустойчивость."""
import copy
import logging

import pytest
from helpers import MANIFEST, make_client, running

from glados_module import ModuleClient, ToolError
from glados_module import protocol as P
from glados_module.testing import FakeCore

PANEL_MANIFEST = copy.deepcopy(MANIFEST)
PANEL_MANIFEST["panel"] = {
    "title": "Демо", "icon": "box",
    "links": [{"id": "docs", "label": "Документы", "kind": "url", "url": "https://example.com"}],
    "actions": ["approve", "refuse_it"],
}


async def wait_panel(core, predicate=None, timeout=3.0):
    await core.wait_for(lambda: core.panels.get("demo") and (predicate is None or predicate(core.panels["demo"])),
                        timeout)
    return core.panels["demo"]


async def test_module_with_bad_panel_connects_without_panel(core, caplog):
    """TAB-1: неверный panel не отклоняет модуль: подключение проходит, в журнал идёт предупреждение."""
    manifest = copy.deepcopy(MANIFEST)
    manifest["panel"] = {"links": [{"id": "x", "label": "x", "kind": "tool", "tool": "nope"}]}
    with caplog.at_level(logging.WARNING, logger="glados_module"):
        client = make_client(core, manifest=manifest)
        async with running(client, core):
            assert client.connected
    assert core.hellos and core.rejected == []
    assert any("пространство модуля отброшено" in r.getMessage() for r in caplog.records)


async def test_panel_set_delivers_blocks_snapshot(core):
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        blocks = [{"type": "stats", "items": [{"label": "Роликов", "value": 2}]}]
        assert await client.panel.set(blocks) is True
        blocks[0]["items"][0]["value"] = 99                 # снимок: уже отправленное не меняется
        got = await wait_panel(core)
        assert got["blocks"][0]["items"][0]["value"] == 2
    assert core.violations == []


async def test_panel_set_rejects_invalid_blocks_on_module_side(core):
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        with pytest.raises(ValueError, match="пространство модуля неверно"):
            await client.panel.set([{"type": "html", "text": "<b>x</b>"}])
        with pytest.raises(ValueError):
            await client.panel.set([{"type": "buttons", "items": [{"label": "x", "action": "undeclared"}]}])
    assert core.panels == {} and core.violations == []


async def test_panel_set_coalesces_fast_updates(core):
    """Не чаще раза в секунду: частые обновления сливаются, уходит последнее значение."""
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        for n in range(5):
            await client.panel.set([{"type": "progress", "value": n / 10}])
        got = await wait_panel(core, lambda b: b["blocks"][0]["value"] == 0.4, timeout=4.0)
        assert got["blocks"][0]["value"] == 0.4
        sent = [m for m in core.messages if m.type == "panel.set"]
        assert len(sent) <= 2                               # первое сразу, остальные слиты в одно отложенное


async def test_panel_set_is_silent_when_disabled(core):
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        client.enabled = False
        assert await client.panel.set([{"type": "text", "text": "тихо"}]) is False
    assert core.panels == {}


async def test_panel_resent_after_reconnect(core):
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        await client.panel.set([{"type": "text", "text": "до обрыва"}])
        await wait_panel(core)
        await core.drop()
        await core.wait_connected(count=2)
        await client.wait_connected(timeout=3)
        await core.wait_for(lambda: len([m for m in core.messages if m.type == "panel.set"]) >= 2, 3.0)
        assert core.panels["demo"]["blocks"][0]["text"] == "до обрыва"


async def test_panel_action_handler_answers(core):
    client = make_client(core, manifest=PANEL_MANIFEST)

    @client.panel_action("approve")
    async def approve(args):
        return {"ok": True, "message": f"одобрено {args['job_id']}", "open_url": "http://127.0.0.1:3000/x"}

    @client.panel_action("refuse_it")
    async def refuse(args):
        raise ToolError("нельзя", code="denied")

    async with running(client, core):
        value = await core.panel_action("approve", {"job_id": "j-1a2b3c4d"})
        assert value == {"ok": True, "message": "одобрено j-1a2b3c4d", "open_url": "http://127.0.0.1:3000/x"}
        with pytest.raises(ToolError) as info:
            await core.panel_action("refuse_it")
        assert info.value.code == "denied"
        with pytest.raises(ToolError) as info:
            await core.panel_action("unknown_action")
        assert info.value.code == "unknown"


async def test_panel_action_returning_nothing_is_success(core):
    client = make_client(core, manifest=PANEL_MANIFEST)

    @client.panel_action("approve")
    async def approve(args):
        return None

    async with running(client, core):
        assert await core.panel_action("approve") == {"ok": True}


async def test_panel_action_bad_return_is_internal_error(core):
    client = make_client(core, manifest=PANEL_MANIFEST)

    @client.panel_action("approve")
    async def approve(args):
        return {"message": "без ok"}

    async with running(client, core):
        with pytest.raises(ToolError) as info:
            await core.panel_action("approve")
        assert info.value.code == "internal"


def test_panel_action_must_be_declared():
    client = ModuleClient(PANEL_MANIFEST, url="ws://127.0.0.1:9/module/ws", token="x")
    with pytest.raises(ValueError, match="panel.actions"):
        client.panel_action("not_declared")(lambda args: None)


async def test_core_flags_invalid_panel_set_from_module(core):
    """FakeCore ведёт себя как ядро: неверный panel.set - нарушение протокола, пространство не сохраняется."""
    client = make_client(core, manifest=PANEL_MANIFEST)
    async with running(client, core):
        await client.wait_connected(timeout=3)
        await client._notify("panel.set", {"blocks": [{"type": "buttons",
                                                      "items": [{"label": "x", "action": "undeclared"}]}]})
        await core.wait_for(lambda: core.violations, 3.0)
    assert core.panels == {} and "panel.set неверен" in core.violations[0]


def test_manifest_dataclass_keeps_panel_for_core():
    spec = P.Manifest.from_dict(PANEL_MANIFEST)
    assert spec.panel is not None and spec.panel.actions == ["approve", "refuse_it"]
    assert spec.panel.links[0].url == "https://example.com"


async def test_fake_core_module_side_panel_set_roundtrip():
    async with FakeCore() as core:
        client = make_client(core, manifest=PANEL_MANIFEST)
        async with running(client, core):
            await client.panel.set([{"type": "link", "label": "Сайт", "url": "https://example.com"}])
            got = await wait_panel(core)
            assert got["blocks"][0]["type"] == "link"
