"""Тесты шаблона на FakeCore: манифест верен, инструменты работают, запросы к ядру идут по контракту."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1])]

from app import build_client  # noqa: E402
from manifest import build_manifest  # noqa: E402

from gits_sdk import protocol as P  # noqa: E402
from gits_sdk.testing import FakeCore  # noqa: E402


@pytest.fixture
async def running():
    async with FakeCore() as core:
        core.install_contract_defaults()                 # образцовые ответы ядра на запросы контракта
        client = build_client(build_manifest(), url=core.url, token=core.token)
        task = asyncio.create_task(client.run())
        await core.wait_connected()
        await client.wait_connected(5)
        try:
            yield core
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


def test_manifest_is_valid():
    assert P.validate_manifest(build_manifest()) == []


async def test_greet_uses_persona_and_emits_event(running):
    out = await running.call_tool("template.greet", {"name": "Аня"})
    assert "Аня" in out
    await running.wait_message("event", lambda b: b["kind"] == "template.greeted")


async def test_remember_fact_goes_through_core_memory(running):
    out = await running.call_tool("template.remember_fact", {"text": "любит чай"})
    assert out["stored"] is True


async def test_empty_arguments_are_rejected(running):
    with pytest.raises(Exception) as e:
        await running.call_tool("template.greet", {"name": " "})
    assert "name" in str(e.value) or "bad_request" in str(e.value)
