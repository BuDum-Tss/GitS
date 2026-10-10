"""Эхо-модуль (MOD-27) против FakeCore: сценарий приёмки ТЗ 01, пункт 3.

Подключение -> repeat -> count с вопросом -> ответ -> done.
"""
import asyncio

import pytest
from echo_module import MANIFEST, build_client

from glados_module import ToolError
from glados_module import protocol as P


@pytest.fixture
def echo(core):
    client = build_client(interval=0.02, url=core.url, token=core.token)
    client.job_progress_interval = 0.0          # в тесте видим каждый шаг
    client.reconnect_delays = (0.05,)
    return client


async def run_echo(client, core):
    task = asyncio.create_task(client.run())
    await core.wait_connected()
    await client.wait_connected(5)
    return task


async def stop_echo(client, task):
    await client.stop()
    await asyncio.wait_for(task, 5)


def test_echo_manifest_is_valid_and_matches_spec():
    assert P.validate_manifest(MANIFEST) == []
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert tools["repeat"]["availability"] == "always" and tools["repeat"]["risk"] == "auto"
    assert tools["count"]["job"] is True and MANIFEST["id"] == "echo"


async def test_acceptance_scenario(core, echo):
    task = await run_echo(echo, core)
    try:
        # repeat
        assert await core.call_tool("echo.repeat", {"text": "привет"}) == "привет"

        # count: подключение -> задача -> вопрос на середине -> ответ -> done
        started = await core.call_tool("echo.count", {"n": 4})
        job_id = started["job_id"]
        assert P.JOB_ID_RE.match(job_id)
        asked = await core.wait_message("job.update", lambda b: b["state"] == "waiting_user")
        assert asked.body["job_id"] == job_id and asked.body["question"]["kind"] == "choice"
        assert asked.body["question"]["options"] == ["продолжать", "остановить"]
        assert asked.body["question"]["question_id"] == "q-1"
        await asyncio.sleep(0.1)
        assert not [m for m in core.messages_of("job.update") if m.body["state"] in ("done", "cancelled")]

        await core.job_answer(job_id, "q-1", "продолжать")
        done = await core.wait_message("job.update", lambda b: b["state"] == "done")
        assert done.body["progress"] == 1.0 and done.body["job_id"] == job_id and "question" not in done.body

        updates = [m.body for m in core.messages_of("job.update")]
        assert updates[0]["state"] == "running" and updates[0]["title"] == "Счёт до 4"
        assert [u["state"] for u in updates][-1] == "done"
        progress = [u["progress"] for u in updates if u["state"] == "running"]
        assert progress == sorted(progress)
        assert all(P.validate_job_update(u) == [] for u in updates)

        event = await core.wait_message("event")
        assert event.body["kind"] == "echo.counted" and event.body["data"]["job_id"] == job_id
        assert core.violations == []
    finally:
        await stop_echo(echo, task)


async def test_count_stopped_by_answer(core, echo):
    task = await run_echo(echo, core)
    try:
        job_id = (await core.call_tool("count", {"n": 6}))["job_id"]
        await core.wait_message("job.update", lambda b: b["state"] == "waiting_user")
        await core.job_answer(job_id, "q-1", "остановить")
        end = await core.wait_message("job.update", lambda b: b["state"] == "cancelled")
        assert end.body["job_id"] == job_id
    finally:
        await stop_echo(echo, task)


async def test_count_cancelled_by_core_and_listed(core, echo):
    task = await run_echo(echo, core)
    try:
        job_id = (await core.call_tool("count", {"n": 6}))["job_id"]
        await core.wait_message("job.update", lambda b: b["state"] == "waiting_user")
        assert [j["state"] for j in await core.job_list()] == ["waiting_user"]
        assert await core.job_cancel(job_id) == {}
        await core.wait_message("job.update", lambda b: b["state"] == "cancelled")
        assert [j["state"] for j in await core.job_list()] == ["cancelled"]
        with pytest.raises(ToolError) as e:
            await core.job_cancel("j-00000000")
        assert e.value.code == "unknown"
    finally:
        await stop_echo(echo, task)


async def test_count_validates_argument(core, echo):
    task = await run_echo(echo, core)
    try:
        for bad in (0, -3, 10001):
            with pytest.raises(ToolError) as e:
                await core.call_tool("count", {"n": bad})
            assert e.value.code == "bad_request"
    finally:
        await stop_echo(echo, task)


async def test_short_count_without_question(core, echo):
    task = await run_echo(echo, core)
    try:
        await core.call_tool("count", {"n": 1})
        await core.wait_message("job.update", lambda b: b["state"] == "done")
        assert not [m for m in core.messages_of("job.update") if m.body["state"] == "waiting_user"]
    finally:
        await stop_echo(echo, task)


async def test_waiting_job_is_resent_after_reconnect(core, echo):
    task = await run_echo(echo, core)
    try:
        job_id = (await core.call_tool("count", {"n": 6}))["job_id"]
        await core.wait_message("job.update", lambda b: b["state"] == "waiting_user")
        await core.drop()
        await core.wait_connected(count=2)
        again = await core.wait_message("job.update", lambda b: b["state"] == "waiting_user", nth=2)   # MOD-22
        assert again.body["job_id"] == job_id and again.body["question"]["question_id"] == "q-1"
        await core.job_answer(job_id, "q-1", "продолжать")
        await core.wait_message("job.update", lambda b: b["state"] == "done")
    finally:
        await stop_echo(echo, task)
