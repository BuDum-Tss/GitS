"""Процесс модуля целиком (ТЗ 11, раздел 11 проверки запуска): main.py как отдельный процесс против FakeCore, без игры.

Модуль должен подключиться, прислать манифест (инструменты MCP-сервера и order), состояние с game_running: false, не падать и
выйти кодом 0 по shutdown. Если на машине сейчас запущена RimWorld, тест пропускается: настоящий модуль начал бы играть."""
import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest
from glados_module.testing import FakeCore

import gamenet

AGENT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(gamenet.port_open(), reason="RimWorld запущена: процесс модуля начал бы играть по-настоящему")
async def test_main_process_connects_reports_state_and_shuts_down(tmp_path):
    own_memory, core_memory = tmp_path / "module-memory", tmp_path / "core-memory"
    own_memory.mkdir()
    core_memory.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RIMWORLD_", "GLADOS_"))}
    env["GLADOS_NO_LAUNCH"] = "1"          # наблюдатель за игрой иначе запустил бы настоящую RimWorld (игры нет => launch_game)
    async with FakeCore(settings={"streamer": False, "dnd": False, "quiet_until": 0.0, "game_model": "haiku",
                                  "claude_usage": "normal", "voice_muted": False, "telegram_after_run": False,
                                  "record_runs": False}) as core:
        env.update(GLADOS_CORE_URL=core.url, GLADOS_MODULE_TOKEN=core.token, GLADOS_MODULE_ID="rimworld",
                   MEMORY_DIR=str(core_memory),                     # так передаёт супервизор: чужая, ядровая папка памяти
                   RIMWORLD_MEMORY_DIR=str(own_memory), RIMWORLD_MODULE_PORT=str(free_port()),
                   GLADOS_FAST_URL="http://127.0.0.1:9/v1/chat/completions",         # локальная модель недоступна
                   GLADOS_DECIDER_URL="http://127.0.0.1:9", PYTHONUNBUFFERED="1",
                   LOCALAPPDATA=str(tmp_path / "la"))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "main.py", cwd=str(AGENT), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            await core.wait_connected(timeout=90)
            hello = core.hellos[0]
            manifest = hello["manifest"]
            assert hello["module_id"] == "rimworld" and manifest["capabilities"] == ["game"]
            names = {t["name"] for t in manifest["tools"]}
            assert "order" in names and "get_colony_state" in names and "launch_game" in names and len(names) > 60
            assert not any(n.startswith("game_") for n in names) and "get_pending_events" not in names
            assert manifest["graph"] is True and manifest["state"] is True

            state = (await core.wait_message("state", timeout=30)).body
            assert state["data"]["game_running"] is False and state["data"]["connected"] is False
            assert state["data"]["policy"]["id"] == "afk"                 # ТЗ 11e: старт вне фокуса (POL-11)
            assert state["summary"].startswith("игра: нет,")
            assert proc.returncode is None                                           # не упал

            assert (await core.request("graph.get", {}))["nodes"]
            from glados_module import ToolError
            with pytest.raises(ToolError) as e:
                await core.call_tool("get_colony_state", {})
            assert e.value.code == "unavailable"                                     # игры нет: честный отказ
            assert await core.call_tool("order", {"text": "построй стену"}) == {"accepted": False}

            await core.shutdown("тест")
            code = await asyncio.wait_for(proc.wait(), 15)
            assert code == 0
        finally:
            if proc.returncode is None:
                proc.kill()
            out = (await proc.stdout.read()).decode("utf-8", "replace") if proc.stdout else ""
    assert "Traceback" not in out, out
    assert "GLADOS_NO_LAUNCH" in out or "RimWorld запускается" not in out, out            # игру модуль не запускал
    assert list(core_memory.iterdir()) == []                                         # ядровая папка памяти не тронута
    assert own_memory.exists()                                                       # своя папка памяти; игру при старте не поднимал
