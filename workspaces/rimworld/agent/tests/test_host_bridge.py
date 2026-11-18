"""HostBridge: локальный мост делегирует существующим модулям (на подставных), фабрика, адрес мода через прокси, импорт без Windows-пакетов."""
import asyncio
import os
import subprocess
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

import game_launcher
import game_watchdog
import gamenet
import host_bridge as hb
import run_recorder as rr

AGENT = Path(__file__).resolve().parent.parent


class FakeProc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class FakeRecorder:
    """Подставной RunRecorder: записывает вызовы."""

    def __init__(self):
        self.current = None
        self.calls = []
        self.alive_result = None

    def start(self, run_id, colony, why=""):
        self.calls.append(("start", run_id, colony, why))
        self.current = rr.Recording(run_id, colony, Path("raw.mp4"), time.time(), FakeProc())

    def close(self, outcome):
        rec, self.current = self.current, None
        rec.outcome = outcome
        rec.duration = 100.0
        return rec

    def ensure_alive(self):
        self.calls.append(("ensure_alive",))
        return self.alive_result

    def ensure_audio(self):
        self.calls.append(("ensure_audio",))
        return True

    async def produce_for_montage(self, rec):
        self.calls.append(("handoff", rec.outcome, rec.markers, rec.goal, rec.events))
        return {"run_id": rec.run_id}

    async def produce(self, rec):
        self.calls.append(("produce", rec.outcome))
        return {"video": "v.mp4"}

    def process(self, rec):
        self.calls.append(("process",))
        return {"video": "again.mp4"}

    def stop_blocking(self, rec):
        self.calls.append(("stop", rec.run_id))


@pytest.fixture
def fake_ui(monkeypatch):
    calls = []
    ui = types.SimpleNamespace(
        screenshot_png=lambda: b"PNG", window_size=lambda: (1920, 1080),
        click=lambda x, y, button="left", clicks=1: calls.append(("click", x, y, button, clicks)),
        move=lambda x, y: calls.append(("move", x, y)),
        drag=lambda *a, button="left": calls.append(("drag", *a, button)),
        type_text=lambda t: calls.append(("type", t)), key_press=lambda k: calls.append(("key", k)),
        scroll=lambda x, y, a: calls.append(("scroll", x, y, a)))
    monkeypatch.setitem(sys.modules, "game_ui", ui)
    ui.calls = calls
    return ui


async def test_status_delegates_to_launcher_and_watchdog(monkeypatch):
    monkeypatch.setattr(game_launcher, "is_running", lambda: True)
    monkeypatch.setattr(game_launcher, "mod_ready", lambda: True)
    monkeypatch.setattr(game_watchdog, "window_state", lambda: "background")
    monkeypatch.setattr(game_watchdog, "user_idle_seconds", lambda: 42.0)
    status = await hb.LocalHostBridge().game_status()
    assert status == {"running": True, "mod_ready": True, "window": "background", "user_idle_s": 42.0,
                      "launch_forbidden": True}                       # GITS_NO_LAUNCH=1 из conftest


async def test_status_without_game_has_missing_window(monkeypatch):
    monkeypatch.setattr(game_launcher, "is_running", lambda: False)
    monkeypatch.setattr(game_watchdog, "user_idle_seconds", lambda: 0.0)
    status = await hb.LocalHostBridge().game_status()
    assert status["running"] is False and status["window"] == "missing" and status["mod_ready"] is False


async def test_launch_passes_windowed_args_and_maps_errors(monkeypatch):
    seen = {}

    def launch(reason, wait_seconds=0, args=()):
        seen.update(reason=reason, wait=wait_seconds, args=args)
        return "ok"
    monkeypatch.setattr(game_launcher, "launch", launch)
    monkeypatch.setattr(game_launcher, "windowed_args", lambda: ("-popupwindow",))
    assert await hb.LocalHostBridge().launch_game("тест", 5.0) == "ok"
    assert seen == {"reason": "тест", "wait": 5.0, "args": ("-popupwindow",)}
    await hb.LocalHostBridge().launch_game("тест", 0, windowed=False)
    assert seen["args"] == ()

    def boom(reason, wait_seconds=0, args=()):
        raise RuntimeError("запуск запрещён")
    monkeypatch.setattr(game_launcher, "launch", boom)
    with pytest.raises(hb.HostBridgeError) as e:
        await hb.LocalHostBridge().launch_game("тест")
    assert e.value.code == "launch_failed"


async def test_quick_start_and_show_window_delegate(monkeypatch):
    shown = []
    monkeypatch.setattr(game_watchdog, "press_quick_start", lambda: True)
    monkeypatch.setattr(game_watchdog, "show_window", lambda: shown.append(1))
    bridge = hb.LocalHostBridge()
    assert await bridge.press_quick_start() is True
    await bridge.show_window()
    assert shown == [1]


async def test_ui_delegates_to_game_ui(fake_ui):
    bridge = hb.LocalHostBridge()
    assert await bridge.ui_screenshot() == b"PNG" and await bridge.ui_window_size() == (1920, 1080)
    await bridge.ui_click(1, 2, "right", 2)
    await bridge.ui_move(3, 4)
    await bridge.ui_drag(1, 2, 3, 4)
    await bridge.ui_type("abc")
    await bridge.ui_key("enter")
    await bridge.ui_scroll(5, 6, -3)
    assert fake_ui.calls == [("click", 1, 2, "right", 2), ("move", 3, 4), ("drag", 1, 2, 3, 4, "left"), ("type", "abc"),
                             ("key", "enter"), ("scroll", 5, 6, -3)]


async def test_missing_window_becomes_clear_error(monkeypatch, fake_ui):
    def no_window(*a, **k):
        raise RuntimeError("Окно RimWorld не найдено")
    fake_ui.click = no_window
    with pytest.raises(hb.HostBridgeError) as e:
        await hb.LocalHostBridge().ui_click(1, 1)
    assert e.value.code == "window_not_found"


async def test_recording_start_tags_and_supersedes_stale():
    bridge = hb.LocalHostBridge()
    rec = bridge._recorder = FakeRecorder()
    first = await bridge.record_start("r1", "К1", "забег")
    assert first["id"] and first["video_alive"] is True and first["run_id"] == "r1"
    second = await bridge.record_start("r2", "К2")                  # прежняя не закрыта: откладывается, новая идёт
    assert second["run_id"] == "r2" and first["id"] in bridge._pending
    assert [c[:2] for c in rec.calls] == [("start", "r1"), ("start", "r2")]


async def test_recording_finish_modes():
    bridge = hb.LocalHostBridge()
    rec = bridge._recorder = FakeRecorder()
    status = await bridge.record_start("r1", "К")
    result = await bridge.record_finish(status["id"], "colony_died", "handoff", markers=[[1.5, "метка", 3]],
                                        goal={"title": "цель"}, events=[{"ts": 1}])
    assert result == {"run_id": "r1"}
    assert rec.calls[-1] == ("handoff", "colony_died", [(1.5, "метка", 3)], {"title": "цель"}, [{"ts": 1}])
    assert await bridge.record_finish(status["id"], "", "montage") == {"video": "again.mp4"}     # уже остановлена: только монтаж
    with pytest.raises(hb.HostBridgeError) as e:
        await bridge.record_finish("нет такой", "x", "stop")
    assert e.value.code == "unknown_record"


async def test_ensure_alive_returns_closed_part_for_later_production():
    bridge = hb.LocalHostBridge()
    rec = bridge._recorder = FakeRecorder()
    await bridge.record_start("r1", "К")
    old = rec.close("part")
    rec.alive_result = old
    rec.current = rr.Recording("r1", "К", Path("raw2.mp4"), time.time(), FakeProc())
    result = await bridge.record_ensure_alive(max_hours=2)
    assert result["closed_id"] and result["status"]["id"] and result["closed_id"] != result["status"]["id"]
    assert (await bridge.record_finish(result["closed_id"], "", "stop")) == {"stopped": True}


async def test_blocking_works_from_worker_and_loop_threads():
    bridge = hb.LocalHostBridge()

    async def value():
        return 7
    assert await asyncio.to_thread(bridge.blocking, value()) == 7
    assert bridge.blocking(value()) == 7                           # из потока цикла: отдельный поток


def test_factory_picks_implementation(monkeypatch):
    monkeypatch.delenv("GITS_RIMWORLD_HOST_URL", raising=False)
    assert isinstance(hb.make_host_bridge(), hb.LocalHostBridge)
    monkeypatch.setenv("GITS_RIMWORLD_HOST_URL", "http://host.docker.internal:8791/")
    monkeypatch.delenv("GITS_RIMWORLD_HOST_TOKEN", raising=False)
    with pytest.raises(ValueError):
        hb.make_host_bridge()
    monkeypatch.setenv("GITS_RIMWORLD_HOST_TOKEN", "секрет-123")
    bridge = hb.make_host_bridge()
    assert isinstance(bridge, hb.RemoteHostBridge) and bridge.url == "http://host.docker.internal:8791"
    assert "секрет-123" not in repr(bridge)


def test_ui_facade_is_remote_adapter_in_container(monkeypatch):
    monkeypatch.setenv("GITS_RIMWORLD_HOST_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("GITS_RIMWORLD_HOST_TOKEN", "t")
    assert isinstance(hb.ui_facade(), hb.SyncUi)


def test_mod_address_through_helper_proxy(monkeypatch):
    monkeypatch.delenv("RIMWORLD_API", raising=False)
    monkeypatch.setenv("GITS_RIMWORLD_HOST_TOKEN", "tok")
    assert gamenet.base_url() == "http://localhost:8765" and not gamenet.is_proxy() and gamenet.auth_headers() == {}
    monkeypatch.setenv("RIMWORLD_API", "http://localhost:9000/")
    assert gamenet.base_url() == "http://localhost:9000" and not gamenet.is_proxy()
    monkeypatch.setenv("RIMWORLD_API", "http://host.docker.internal:8791/game-api")
    assert gamenet.is_proxy() and gamenet.base_url() == "http://host.docker.internal:8791/game-api"
    assert gamenet.auth_headers() == {"Authorization": "Bearer tok"}


def test_proxy_port_check_is_a_ping_through_the_helper(monkeypatch):
    monkeypatch.setenv("RIMWORLD_API", "http://host.docker.internal:8791/game-api")
    monkeypatch.setattr(gamenet, "_proxy_ping", lambda timeout: True)
    assert gamenet.port_open() is True
    monkeypatch.setattr(gamenet, "_proxy_ping", lambda timeout: False)
    assert gamenet.port_open() is False


def test_module_imports_without_windows_packages(tmp_path):
    """Режим Remote (контейнер): main, контроллер и инструменты импортируются, когда Windows-пакетов нет."""
    code = textwrap.dedent("""
        import sys, types
        import mcp.server.fastmcp                       # сторонний mcp на Windows сам тянет win32con: грузим до блокировки (в Linux-образе его нет)
        for name in ("pyautogui", "win32gui", "win32con", "win32print", "psutil", "soundcard", "pygetwindow"):
            sys.modules[name] = None
        sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
        import main, controller, host_bridge, remote_recorder, game_launcher, game_watchdog, run_recorder, rimworld_mcp
        bridge = host_bridge.make_host_bridge()
        assert isinstance(bridge, host_bridge.RemoteHostBridge)
        assert isinstance(rimworld_mcp._ensure_game_ui(), host_bridge.SyncUi)
        assert "game_ui" not in sys.modules
        assert game_watchdog.user_idle_seconds() == 0.0 or sys.platform == "win32"
        print("OK")
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RIMWORLD_", "GLADOS_"))}
    env.update(GITS_RIMWORLD_HOST_URL="http://host.docker.internal:8791", GITS_RIMWORLD_HOST_TOKEN="t",
               RIMWORLD_MEMORY_DIR=str(tmp_path), GITS_NO_LAUNCH="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(AGENT), env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and "OK" in done.stdout, done.stderr[-1500:]


async def test_watchdog_takes_window_and_idle_from_host(monkeypatch):
    """Наблюдатель с `host`: процесс игры, окно и бездействие пользователя берутся у моста, прямых вызовов Windows нет."""
    import planning_pause
    monkeypatch.setenv("GITS_RIMWORLD_KEEP_WINDOW", "1")           # прежнее поведение: сторож возвращает свёрнутое окно

    class Host(hb.HostBridge):
        shown = 0

        async def game_status(self):
            return {"running": True, "mod_ready": True, "window": "minimized", "user_idle_s": 500.0}

        async def show_window(self):
            Host.shown += 1

    def forbidden(*a, **k):
        raise AssertionError("прямой вызов Windows при заданном host")
    monkeypatch.setattr(game_launcher, "is_running", forbidden)
    monkeypatch.setattr(game_watchdog, "window_state", forbidden)
    monkeypatch.setattr(game_watchdog, "user_idle_seconds", forbidden)
    monkeypatch.setattr(game_watchdog, "show_window", forbidden)
    monkeypatch.setattr(planning_pause, "active", lambda *a, **k: False)
    state = {"tick": 5, "paused": True, "time_speed": 0}
    wd = game_watchdog.GameWatchdog(
        settings_get=lambda: {"record_runs": True}, recorder=None, launch=lambda: None, new_colony=None,
        mod_ready=lambda: True, state_ready=lambda: True, send_camera=lambda c: None, on_camera_lost=lambda: None,
        get_state=lambda: dict(state), send_command=lambda c: True, host=Host(), clock=lambda: 1000.0)
    snap = await asyncio.to_thread(wd.snapshot)
    assert snap["game_running"] and snap["window"] == "minimized"
    assert wd._idle() == 500.0
    acts = await wd.step()
    assert "show_window" in acts and Host.shown == 1
