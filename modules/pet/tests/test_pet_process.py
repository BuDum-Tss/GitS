"""Процесс модуля: main.py без окна (GITS_PET_HEADLESS=1) подключается к FakeCore, манифест принят, выход по shutdown.

Проверка настоящего окна: GITS_PET_WINDOW_TEST=1 (на экране на пару секунд появится питомец; нужен свободный мьютекс питомца).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from gits_sdk.testing import FakeCore

MODULE_DIR = Path(__file__).resolve().parent.parent
GITS = MODULE_DIR.parent.parent
SETTINGS = {"voice_muted": False, "pet_visible": True, "pet_pinned": False, "pet_x": None, "pet_y": None,
            "hotkey_input": "Ctrl+Shift+Space", "dnd": False, "quiet_until": 0.0}


async def run_pet(tmp_path, headless: bool, run_s: float = 0.0):
    async with FakeCore(settings=SETTINGS) as core:
        env = dict(os.environ)
        env.update(GITS_CORE_URL=core.url, GITS_MODULE_TOKEN=core.token, GITS_MODULE_ID="pet",
                   LOCALAPPDATA=str(tmp_path / "la"), PYTHONUNBUFFERED="1", PYTHONPATH=str(GITS / "sdk"))
        if headless:
            env["GITS_PET_HEADLESS"] = "1"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "main.py", cwd=str(MODULE_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            await core.wait_connected(timeout=30)
            hello = core.hellos[0]
            assert hello["module_id"] == "pet" and hello["manifest"]["capabilities"] == ["presence.view"]
            assert core.rejected == [] and proc.returncode is None
            await core.topic("presence", {"speaking": True, "segment": "Привет.", "emotion": "happy"})
            await asyncio.sleep(run_s or 0.5)
            assert proc.returncode is None
            await core.shutdown("тест")
            code = await asyncio.wait_for(proc.wait(), 15)
            assert code == 0
        finally:
            if proc.returncode is None:
                proc.kill()
            out = (await proc.stdout.read()).decode("utf-8", "replace") if proc.stdout else ""
    assert "Traceback" not in out, out


async def test_main_process_headless_connects_and_shuts_down(tmp_path):
    await run_pet(tmp_path, headless=True)


@pytest.mark.skipif(os.getenv("GITS_PET_WINDOW_TEST") != "1", reason="настоящее окно: только вручную (GITS_PET_WINDOW_TEST=1)")
async def test_main_process_with_a_real_window(tmp_path):
    await run_pet(tmp_path, headless=False, run_s=3.0)
