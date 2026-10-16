"""Набор проверок на соответствие контракту (ТЗ 31, MC-7).

Одни и те же примеры (`valid`, `invalid`, `response_valid`, `response_invalid` из `gits_sdk.contract.messages`) прогоняются через
проверки SDK и через `FakeCore`; те же примеры прогоняет ядро в `agent/tests/test_contract_conformance_core.py`
(настоящие `module_hub` и `core_requests`). Все три обязаны отвечать одинаково: неверное тело - `bad_request`,
верное - не `bad_request`, запрос со статусом planned - `unavailable`.
"""
import asyncio

import pytest
from helpers import make_client

from gits_sdk import ToolError, contract
from gits_sdk import protocol as P
from gits_sdk.contract import messages as M
from gits_sdk.protocol import Envelope
from gits_sdk.testing import FakeCore

ALL = list(contract.REGISTRY.values())
REQUESTS = [m for m in M.CORE_REQUEST_MESSAGES]
IMPLEMENTED = [m for m in REQUESTS if m.status != contract.PLANNED]
PLANNED = [m for m in REQUESTS if m.status == contract.PLANNED]
NOTES_CHECKED = ("event", "say", "job.update", "log", "emotion", "state")


def ids(msgs):
    return [m.name for m in msgs]


# ── полнота контракта ───────────────────────────────────────────────────────────

def test_every_protocol_message_is_described():
    """Каждый тип сообщения протокола описан в контракте и наоборот (кроме служебного `result` и `topic`)."""
    described = set(contract.REGISTRY)
    wire = set(P.MODULE_TO_CORE) | set(P.CORE_TO_MODULE)
    assert wire - described <= {"result", "topic"}, wire - described
    assert set(P.CORE_REQUESTS) == set(contract.CORE_REQUESTS)
    assert set(P.CORE_TO_MODULE_REQUESTS) == set(contract.MODULE_REQUESTS)
    assert set(P.TOPICS) - {"module"} <= set(contract.TOPICS) and set(contract.TOPICS) <= set(P.TOPICS)


def test_acceptance_counts():
    assert len(contract.CORE_REQUESTS) >= 12 and len(contract.MODULE_REQUESTS) == 11
    base12 = {"core.ask", "core.tts", "core.tool.call", "core.persona", "core.privacy_terms", "core.claude_usage",
              "core.web_search", "core.lookup", "core.memory.recall", "core.memory.remember", "core.experience.add",
              "core.kb.ingest"}
    assert base12 <= set(contract.CORE_REQUESTS)


@pytest.mark.parametrize("m", ALL, ids=ids(ALL))
def test_every_element_has_complete_description(m):
    assert m.doc and m.direction in (contract.M2C, contract.C2M) and m.kind in (contract.REQUEST, contract.NOTE, contract.TOPIC)
    assert m.status in (contract.STABLE, contract.PLANNED, "deprecated") and m.since
    if m.kind == contract.REQUEST:
        assert m.errors, f"{m.name}: нужны коды ошибок"
    if m.status == contract.PLANNED:
        assert m.planned_in
    assert m.valid or m.name == "hello", f"{m.name}: нужен пример верного тела"
    assert m.invalid, f"{m.name}: нужен пример неверного тела"
    for e in m.errors:
        assert e in P.ERROR_CODES


# ── проверки SDK ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("m", ALL, ids=ids(ALL))
def test_sdk_accepts_valid_and_rejects_invalid_bodies(m):
    ctx = {"actions": set()} if m.name == "panel.set" else {}
    for body in m.valid:
        assert contract.validate(m.name, body, **ctx) == [], (m.name, body)
    for body in m.invalid:
        assert contract.validate(m.name, body, **ctx), (m.name, body)


@pytest.mark.parametrize("m", [m for m in ALL if m.response or m.response_valid or m.response_invalid],
                         ids=lambda m: m.name)
def test_sdk_checks_responses(m):
    for value in m.response_valid:
        assert contract.validate_response(m.name, value) == [], (m.name, value)
    for value in m.response_invalid:
        assert contract.validate_response(m.name, value), (m.name, value)


def test_unknown_name_is_reported():
    assert contract.validate("core.nothing", {})
    assert contract.validate_topic("nothing", {})


def test_json_schemas_are_built_for_every_request():
    for m in ALL:
        if m.request:
            s = m.request_schema()
            assert s["type"] == "object" and set(s["properties"]) == {f.name for f in m.request}
            assert set(s.get("required", [])) == {f.name for f in m.request if f.required}


def test_extra_fields_are_tolerated():
    """MC-8: новое необязательное поле в теле не ломает старого получателя."""
    assert contract.validate("core.tts", {"text": "x", "future_field": [1, 2]}) == []
    assert contract.validate("event", {"kind": "demo.b", "text": "x", "future": 1}) == []


# ── FakeCore строится из контракта ──────────────────────────────────────────────

async def _raw(core, msg_type, body):
    """Послать запрос от имени модуля без проверок клиента; вернуть тело result."""
    client = make_client(core)
    task = asyncio.create_task(client.run())
    try:
        await core.wait_connected()
        env = Envelope(msg_type, body)
        fut = asyncio.get_running_loop().create_future()
        client._pending[env.id] = fut
        await client._send_env(env)
        reply = await asyncio.wait_for(fut, 5)
        return reply.body
    finally:
        await client.stop()
        await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("m", IMPLEMENTED, ids=ids(IMPLEMENTED))
async def test_fakecore_answers_like_the_core_for_requests(m):
    async with FakeCore() as core:
        core.install_contract_defaults()
        client = make_client(core)
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            for body in m.invalid:
                if not isinstance(body, dict):
                    continue
                with pytest.raises(ToolError) as e:
                    await client.request(m.name, body)
                assert e.value.code == "bad_request", (m.name, body)
            for body in m.valid:
                value = await client.request(m.name, body)               # образцовый ответ из контракта
                assert contract.validate_response(m.name, value) == [], (m.name, value)
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("m", PLANNED, ids=ids(PLANNED))
async def test_fakecore_marks_planned_requests_unavailable(m):
    async with FakeCore() as core:
        client = make_client(core)
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            for body in (*m.valid, {}):
                with pytest.raises(ToolError) as e:
                    await client.request(m.name, body)
                assert e.value.code == "unavailable" and contract.CONTRACT_VERSION in e.value.message
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


@pytest.mark.parametrize("name", NOTES_CHECKED)
async def test_fakecore_checks_notes_with_the_same_contract(name):
    m = contract.get(name)
    async with FakeCore() as core:
        client = make_client(core)
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            for body in m.valid:
                await client._notify(name, body)
            await core.wait_for(lambda: len(core.messages_of(name)) >= len(m.valid))
            assert core.violations == [], core.violations
            for body in (b for b in m.invalid if isinstance(b, dict)):
                await client._notify(name, body)
            n = len([b for b in m.invalid if isinstance(b, dict)])
            await core.wait_for(lambda: len(core.violations) >= n)
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


async def test_fakecore_collects_log_messages():
    async with FakeCore() as core:
        client = make_client(core)
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            client.log("error", "сбой", code=3)
            client.log("user", "чужой вид")                                   # недопустимый вид отбрасывается у отправителя
            await core.wait_for(lambda: core.logs)
            assert core.logs == [{"kind": "error", "text": "сбой", "data": {"code": 3}}]
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


async def test_log_waits_for_the_connection():
    async with FakeCore() as core:
        client = make_client(core)
        client.log("telegram", "до подключения")
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            await core.wait_for(lambda: core.logs)
            assert core.logs[0]["text"] == "до подключения"
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


# ── режим разработки: клиент проверяет входящее ─────────────────────────────────

async def test_strict_client_rejects_malformed_core_requests():
    async with FakeCore() as core:
        client = make_client(core)
        client.strict = True
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            with pytest.raises(ToolError) as e:
                await core.request("deliver", {"text": ""})
            assert e.value.code == "bad_request"
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


async def test_strict_client_rejects_malformed_core_responses():
    async with FakeCore() as core:
        core.handlers["core.tts"] = lambda body: {"path": "x"}              # нет duration_ms
        client = make_client(core)
        client.strict = True
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            with pytest.raises(ToolError) as e:
                await client.tts("привет")
            assert e.value.code == "internal"
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)


# ── типизированный клиент ───────────────────────────────────────────────────────

async def test_typed_client_methods_use_the_contract():
    async with FakeCore() as core:
        core.install_contract_defaults()
        core.handlers["core.privacy_terms"] = lambda body: {"terms": ["Аня"]}
        client = make_client(core)
        task = asyncio.create_task(client.run())
        try:
            await core.wait_connected()
            assert (await client.tts("привет", "joy", "ogg")).duration_ms == 1200
            assert (await client.persona()).name
            assert await client.privacy_terms() == ["Аня"]
            assert (await client.llm_ask("s", "u")).text
            assert await client.events_tail(kinds={"game"}) == []
            assert (await client.call_tool("echo.repeat", {"text": "а"})).text
            await client.claude_usage(10, "t")
            with pytest.raises(ValueError):
                await client.tts("")                                          # неверное исходящее тело
            assert isinstance(await client.lookup("что-то"), str)                 # справочник реализован (контракт 1.4)
        finally:
            await client.stop()
            await asyncio.wait_for(task, 5)
