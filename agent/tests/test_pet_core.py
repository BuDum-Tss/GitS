"""Ядро и питомец-модуль (ТЗ 13: PET-9, PET-10, PET-11): запись в modules.yaml, {PYTHONW}, трей не запускает питомца,
дашборд без say.json и поля say, presence с emotion_ts, настоящий процесс питомца (без окна) под супервизором."""
import asyncio
import re
import time
from pathlib import Path

import aiohttp
import psutil
from aiohttp import web
from test_core_wiring import WiredCompanion, free_port
from test_dashboard_endpoints import serve

import core_service
import dashboard
import settings
import speech_out
from environments.registry import Registry
from supervisor import Supervisor, load_specs

AGENT = Path(__file__).resolve().parent.parent
GITS = AGENT.parent


def test_modules_yaml_has_the_pet_started_with_pythonw():
    pet = {s.id: s for s in load_specs()}["pet"]
    assert (pet.command, pet.args, pet.autostart, pet.restart) == ("{PYTHONW}", ["main.py"], True, "on-failure")
    assert pet.cwd == "{GITS}/modules/pet" and pet.kind == "module"
    sup = Supervisor([pet], token="t" * 64, core_url="ws://x")
    command, cwd = sup.resolve("pet")
    assert Path(command[0]).name.lower() in ("pythonw.exe", "python.exe") and command[1:] == ["main.py"]
    assert cwd == GITS / "modules" / "pet" and (cwd / "main.py").exists()


def test_pythonw_placeholder_falls_back_to_python_without_pythonw(tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_text("")
    sup = Supervisor([], token="t" * 64, core_url="ws://x", python=str(fake))
    assert sup.pythonw() == str(fake)
    (tmp_path / "pythonw.exe").write_text("")
    assert sup.pythonw() == str(tmp_path / "pythonw.exe")


def test_tray_does_not_start_the_pet_any_more():
    src = (AGENT / "tray.py").read_text(encoding="utf-8")
    assert "desktop_pet" not in src and "start_pet" not in src
    assert re.search(r"def _toggle_pet.*?settings\.update\(pet_visible", src, re.S)                    # пункт трея остался
    assert "modules" in src and "pet" in src
    assert not (AGENT / "desktop_pet.py").exists() and not (AGENT / "quick_input.py").exists()
    assert not (AGENT / "check_stickers.py").exists() and not (AGENT / "static" / "stickers").exists()
    assert (GITS / "modules" / "pet" / "static" / "stickers" / "idle.png").exists()


def test_legacy_pet_speech_and_say_json_are_gone():
    assert not hasattr(speech_out, "LegacyPetSpeech")
    for path in AGENT.glob("*.py"):
        assert "say.json" not in path.read_text(encoding="utf-8") or path.name == "speech_out.py", path.name


async def test_state_endpoint_has_presence_with_emotion_time_and_no_say(tmp_path):
    port, layer, runner = await serve(tmp_path)
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{port}/api/state") as r:
                state = await r.json()
        assert "say" not in state and "last_decision" not in state
        assert state["presence"]["emotion_ts"] == 0.0 and "settings" in state
        assert not (tmp_path / "memory" / "say.json").exists()
    finally:
        await layer.stop()
        await runner.cleanup()


async def test_say_does_not_write_say_json(tmp_path):
    from companion import Companion
    c = Companion(str(tmp_path / "memory"), rel=None)
    await c.say("Привет")
    assert not (tmp_path / "memory" / "say.json").exists()


async def test_real_pet_process_started_by_the_supervisor_gets_presence_and_stops(tmp_path, monkeypatch):
    """Настоящий процесс modules/pet (GITS_PET_HEADLESS=1: окна нет), запущенный супервизором: подключается, манифест принят,
    получает presence с эмоцией, выключается за <= 10 с. pythonw здесь подменён на python (вывод процесса виден в журнале)."""
    monkeypatch.setenv("GITS_PET_HEADLESS", "1")
    spec = [s for s in load_specs() if s.id == "pet"][0]
    companion = WiredCompanion()
    port = free_port()
    layer = core_service.wire_modules(companion, Registry(), port=port, specs=[spec],
                                      battery=lambda: {"level": 1.0, "sleeping": False})
    dashboard._MEMORY_DIR = tmp_path / "memory"
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    app = web.Application()
    layer.hub.attach(app)
    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    await layer.start()
    pid = None

    async def wait(predicate, timeout=30.0):
        async def waiter():
            while not predicate():
                await asyncio.sleep(0.02)
        await asyncio.wait_for(waiter(), timeout)
    try:
        await wait(lambda: layer.hub.is_connected("pet"))
        rec = layer.modules.get("pet")
        pid = rec.pid
        assert rec.manifest["capabilities"] == ["presence.view"] and rec.manifest["subscribe"] == ["presence", "settings", "module"]
        companion.presence.update(emotion="angry", emotion_ts=time.time())             # presence доходит до процесса без ошибок
        await asyncio.sleep(0.5)
        assert layer.hub.is_connected("pet") and psutil.pid_exists(pid)
        settings.update(modules={"pet": {"enabled": False}})
        layer.modules.invalidate()
        t0 = time.monotonic()
        await layer.supervisor.apply("pet")
        await wait(lambda: not psutil.pid_exists(pid), 15)
        assert time.monotonic() - t0 <= 10
        log = layer.supervisor.log_path("pet")
        assert "Traceback" not in (log.read_text(encoding="utf-8", errors="replace") if log.exists() else "")
    finally:
        await layer.stop()
        await runner.cleanup()
        if pid and psutil.pid_exists(pid):
            psutil.Process(pid).kill()
