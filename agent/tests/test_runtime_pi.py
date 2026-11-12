"""Среда pi (`adapters/runtime_pi.py`) против поддельного моста на Python (`fake_pi_bridge.py`): без Node, сети и моделей."""
import asyncio
import logging
import sys
from pathlib import Path

import pytest

import contracts
from adapters.runtime_pi import PiRuntime
from contracts import EndEvent, TextEvent, ToolEvent, ToolSpec

BRIDGE = Path(__file__).with_name("fake_pi_bridge.py")


async def _find(args):
    return True, "найдено " + str(args.get("query"))


SPEC = ToolSpec("find_tools", "поиск", {"type": "object", "properties": {"query": {"type": "string"}}}, _find)


def _make(tmp_path, auth=True, tools=None):
    auth_file = tmp_path / "auth.json"
    if auth:
        auth_file.write_text("{}", encoding="utf-8")
    rt = PiRuntime(command=[sys.executable, str(BRIDGE)], auth_file=str(auth_file), session_file=tmp_path / "session.txt")
    return rt, tools if tools is not None else [SPEC]


async def _collect(rt, prompt):
    return [e async for e in rt.run(prompt)]


@pytest.fixture
async def pi(tmp_path):
    rt, tools = _make(tmp_path)
    await rt.start("система", tools)
    yield rt
    await rt.close()


def test_pi_runtime_satisfies_the_contract():
    assert isinstance(PiRuntime(), contracts.AgentRuntime) and PiRuntime.can_steer is True


async def test_text_and_end_with_usage(pi):
    events = await _collect(pi, "hello")
    assert events == [TextEvent("Привет"), EndEvent("ok", None, {"requests": 1, "input": 5, "output": 7, "cache_read": 0})]


async def test_long_line_from_the_bridge_is_read_whole(pi):
    events = await _collect(pi, "big")
    assert isinstance(events[0], TextEvent) and len(events[0].text) == 300000 and events[-1].status == "ok"


async def test_tool_call_runs_the_core_handler_and_returns_its_result(pi):
    events = await _collect(pi, "tool")
    assert events[0] == ToolEvent("find_tools", {"query": "tool"})
    assert events[1] == TextEvent("ok=True найдено tool") and events[-1].status == "ok"


async def test_failed_tool_result_is_reported_as_not_ok(tmp_path):
    async def bad(args):
        return False, "НЕ ПОЛУЧИЛОСЬ"
    rt, _ = _make(tmp_path)
    await rt.start("s", [ToolSpec("find_tools", "x", {"type": "object"}, bad)])
    try:
        events = await _collect(rt, "tool")
        assert TextEvent("ok=False НЕ ПОЛУЧИЛОСЬ") in events
    finally:
        await rt.close()


async def test_handler_exception_does_not_kill_the_turn(tmp_path):
    async def boom(args):
        raise ValueError("сломалось")
    rt, _ = _make(tmp_path)
    await rt.start("s", [ToolSpec("find_tools", "x", {"type": "object"}, boom)])
    try:
        events = await _collect(rt, "tool")
        assert any(isinstance(e, TextEvent) and e.text.startswith("ok=False") and "сломалось" in e.text for e in events)
        assert events[-1].status == "ok"
    finally:
        await rt.close()


async def test_steer_reaches_the_running_turn(pi):
    seen: list = []

    async def go():
        async for e in pi.run("wait"):
            seen.append(e)
    task = asyncio.create_task(go())
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.05)
    assert seen == [TextEvent("работаю")]
    assert await pi.steer("а ещё привет") is True
    await asyncio.wait_for(task, 10)
    assert TextEvent("подмешано: а ещё привет") in seen and seen[-1].status == "ok"


async def test_steer_without_a_running_turn_is_refused(pi):
    assert await pi.steer("никого нет") is False                 # ещё и мост не поднят
    await _collect(pi, "hello")
    assert await pi.steer("ход кончился") is False


async def test_bridge_idle_answer_means_steer_refused(pi):
    """Ход «идёт» только у ядра (мост его закончил): мост отвечает idle, steer возвращает False."""
    await _collect(pi, "hello")
    pi._events = asyncio.Queue()                                 # ядро ещё думает, что ход идёт
    try:
        assert await pi.steer("поздно") is False
    finally:
        pi._events = None


async def test_abort_ends_the_turn_cancels_the_waiting_tool_and_keeps_working(tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(args):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return True, "никогда"
    rt, _ = _make(tmp_path)
    await rt.start("s", [ToolSpec("find_tools", "x", {"type": "object"}, slow)])
    try:
        seen: list = []

        async def go():
            async for e in rt.run("slowtool"):
                seen.append(e)
        task = asyncio.create_task(go())
        await asyncio.wait_for(started.wait(), 10)
        await rt.abort()
        await asyncio.wait_for(task, 10)
        assert seen[-1] == EndEvent("aborted", None, {"requests": 1, "input": 0, "output": 0, "cache_read": 0})
        assert cancelled.is_set()
        assert (await _collect(rt, "hello"))[-1].status == "ok"   # тот же процесс, следующий ход работает
    finally:
        await rt.close()


async def test_bridge_death_ends_the_turn_with_error_and_the_next_turn_restarts_it(pi):
    events = await _collect(pi, "die")
    assert isinstance(events[-1], EndEvent) and events[-1].status == "error" and "заверш" in events[-1].error
    assert (await _collect(pi, "hello"))[-1].status == "ok"


async def test_missing_login_is_an_error_event_not_an_exception(tmp_path):
    rt, tools = _make(tmp_path, auth=False)
    await rt.start("s", tools)
    try:
        events = await _collect(rt, "hello")
        assert len(events) == 1 and events[0].status == "error" and "нет входа" in events[0].error
    finally:
        await rt.close()


async def test_missing_node_is_an_error_event(tmp_path, monkeypatch):
    import adapters.runtime_pi as mod
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    rt = PiRuntime(auth_file=str(tmp_path / "a.json"), session_file=tmp_path / "s.txt")
    await rt.start("s", [SPEC])
    events = await _collect(rt, "hello")
    assert events[0].status == "error" and "node" in events[0].error


async def test_changed_system_prompt_is_sent_before_the_next_turn(pi):
    await _collect(pi, "hello")
    await pi.start("новая система", [SPEC])
    events = await _collect(pi, "system")
    assert events[0] == TextEvent("system=новая система")


async def test_history_file_is_restored_after_restart_and_forgotten_on_reset(tmp_path):
    rt, tools = _make(tmp_path)
    await rt.start("s", tools)
    await _collect(rt, "hello")
    await _collect(rt, "hello")
    await rt.close()
    again, _ = _make(tmp_path)
    await again.start("s", tools)
    try:
        await _collect(again, "hello")
        assert again.restored == 2
        await again.reset(forget=True)
        assert not (tmp_path / "session.txt").exists()
    finally:
        await again.close()


async def test_bridge_stderr_goes_to_the_log(tmp_path, caplog):
    rt, tools = _make(tmp_path)
    await rt.start("s", tools)
    caplog.set_level(logging.INFO, logger="resident")
    try:
        await _collect(rt, "hello")
        await asyncio.sleep(0.2)
        assert "fake bridge ready" in caplog.text
    finally:
        await rt.close()
