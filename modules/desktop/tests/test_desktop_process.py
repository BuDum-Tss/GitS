"""Процесс модуля: main.py стартует (GITS_DESKTOP_FAKE=1: экран, ввод и Claude не трогаются), подключается к FakeCore,
шлёт состояние, честно отказывает в действиях с экраном и выходит по shutdown с кодом 0."""
import asyncio
import os
import sys
from pathlib import Path

from gits_sdk import ToolError
from gits_sdk.testing import FakeCore

MODULE_DIR = Path(__file__).resolve().parent.parent
GITS = MODULE_DIR.parent.parent


async def test_main_process_connects_reports_state_and_exits_on_shutdown(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    async with FakeCore(settings={"vision": False, "auto_accept": False, "dnd": False, "quiet_until": 0.0}) as core:
        env = dict(os.environ)
        env.update(GITS_CORE_URL=core.url, GITS_MODULE_TOKEN=core.token, GITS_MODULE_ID="desktop",
                   MEMORY_DIR=str(mem), GITS_SECRETS_FILE=str(tmp_path / "нет-secrets.env"),
                   LOCALAPPDATA=str(tmp_path / "la"), GITS_DESKTOP_FAKE="1",
                   PYTHONUNBUFFERED="1", PYTHONPATH=str(GITS / "sdk"))
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "main.py", cwd=str(MODULE_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out = ""
        try:
            await core.wait_connected(timeout=30)
            hello = core.hellos[0]
            assert hello["module_id"] == "desktop" and hello["manifest"]["capabilities"] == ["screen.observe"]
            state = (await core.wait_message("state", timeout=30)).body
            assert state["data"]["foreground_app"] is None and state["data"]["sensitive"] is False
            assert proc.returncode is None

            try:
                await core.call_tool("take_screenshot", {"target": "game"}, timeout=15)
                raise AssertionError("в тестовом режиме снимок не должен получиться")
            except ToolError as e:
                assert e.code == "unavailable"
            assert (await core.call_tool("last_screenshot", timeout=15)) == {"path": None}

            await core.shutdown("тест")
            code = await asyncio.wait_for(proc.wait(), 15)
            assert code == 0
        finally:
            if proc.returncode is None:
                proc.kill()
            out = (await proc.stdout.read()).decode("utf-8", "replace") if proc.stdout else ""
    assert "Traceback" not in out, out
