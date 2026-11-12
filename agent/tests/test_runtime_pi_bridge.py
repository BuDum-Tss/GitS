"""Стыковка `PiRuntime` с настоящим мостом Node (`services/agent-runtime/bridge.mjs`) на поддельном поставщике, без сети.

Тесты адаптера в `test_runtime_pi.py` идут против подделки моста на Python; здесь проверяется, что обе стороны протокола
совпадают на деле. Нет Node или не выполнен `npm install` в папке моста: тесты пропускаются.
"""
import asyncio
import shutil

import pytest

from adapters.runtime_pi import BRIDGE_DIR, PiRuntime
from contracts import EndEvent, TextEvent, ToolEvent, ToolSpec

pytestmark = pytest.mark.skipif(not shutil.which("node") or not (BRIDGE_DIR / "node_modules").is_dir(),
                                reason="нет node или зависимостей моста")
SCHEMA = {"type": "object", "properties": {"tool_id": {"type": "string"}}, "required": ["tool_id"]}


def _runtime(tmp_path, responses, handler):
    rt = PiRuntime(provider="faux", faux_responses=responses, session_file=tmp_path / "session.json")
    return rt, [ToolSpec("call_tool", "вызвать инструмент", SCHEMA, handler)]


async def _collect(rt, prompt):
    return [e async for e in rt.run(prompt)]


def test_tool_roundtrip_and_restore(tmp_path):
    async def go():
        calls = []

        async def handler(args):
            calls.append(args)
            return True, "готово"

        rt, tools = _runtime(tmp_path, [{"text": "Делаю.", "tool_calls": [{"name": "call_tool", "args": {"tool_id": "a.b"}}]},
                                         {"text": "Сделала."}], handler)
        await rt.start("система", tools)
        events = await _collect(rt, "сделай")
        await rt.close()
        assert calls == [{"tool_id": "a.b"}]
        assert [e.text for e in events if isinstance(e, TextEvent)] == ["Делаю.", "Сделала."]
        assert [e.name for e in events if isinstance(e, ToolEvent)] == ["call_tool"]
        assert isinstance(events[-1], EndEvent) and events[-1].status == "ok" and events[-1].usage["requests"] == 2
        again, tools = _runtime(tmp_path, [{"text": "Помню."}], handler)      # новый процесс моста: история с диска
        await again.start("система", tools)
        events = await _collect(again, "что было?")
        await again.close()
        assert again.restored >= 4 and events[-1].status == "ok"
    asyncio.run(go())


def test_steer_reaches_running_turn_and_abort_keeps_history(tmp_path):
    async def go():
        gate = asyncio.Event()

        async def handler(args):
            await gate.wait()
            return True, "готово"

        rt, tools = _runtime(tmp_path, [{"tool_calls": [{"name": "call_tool", "args": {"tool_id": "long"}}]},
                                         {"text": "Вижу сообщение."},
                                         {"tool_calls": [{"name": "call_tool", "args": {"tool_id": "long2"}}]},
                                         {"text": "После обрыва."}], handler)
        await rt.start("система", tools)
        assert await rt.steer("рано") is False                      # хода нет: подмешивать некуда
        events = []

        async def turn(prompt):
            async for e in rt.run(prompt):
                events.append(e)
                if isinstance(e, ToolEvent):
                    asyncio.get_running_loop().create_task(on_tool())

        async def on_tool():
            if not gate.is_set() and len([e for e in events if isinstance(e, ToolEvent)]) == 1:
                assert await rt.steer("[чат, данные] привет") is True
                gate.set()

        await turn("работай")
        assert events[-1].status == "ok" and [e.text for e in events if isinstance(e, TextEvent)] == ["Вижу сообщение."]
        gate.clear()
        events.clear()

        async def abort_soon():
            while not any(isinstance(e, ToolEvent) for e in events):
                await asyncio.sleep(0.01)
            await rt.abort()

        aborter = asyncio.get_running_loop().create_task(abort_soon())
        async for e in rt.run("ещё работай"):
            events.append(e)
        await aborter
        assert events[-1].status == "aborted"
        events.clear()
        async for e in rt.run("дальше"):                               # та же история, мост жив
            events.append(e)
        await rt.close()
        assert events[-1].status == "ok" and [e.text for e in events if isinstance(e, TextEvent)] == ["После обрыва."]
    asyncio.run(go())
