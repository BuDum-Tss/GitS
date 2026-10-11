"""Модуль desktop против FakeCore (ТЗ 14, DESK-17): события, состояние, разрешения, задача Claude, тестовый режим.

Без экрана, мыши, OCR, LM Studio и Claude: окно, простой, анализ и помощник подменены. Тики опроса вызываются напрямую
(модуль не запускает свой цикл в тестах), часы подменены."""
import asyncio
import re

import pytest
from glados_module import ModuleClient, ToolError
from glados_module.protocol import validate_manifest
from glados_module.testing import FakeCore

import app as appmod
import pc_control
import screenshots
from app import DesktopModule
from manifest import build_manifest

SETTINGS = {"vision": True, "auto_accept": False, "dnd": False, "quiet_until": 0.0}


class Scene:
    """Подменённый экран: активное окно, простой ввода и часы."""

    def __init__(self):
        self.win = {"title": "Документ - Блокнот", "app": "notepad", "rect": (0, 0, 800, 600)}
        self.idle = 0.0
        self.now = 1000.0

    def fg(self):
        return self.win

    def idle_fn(self):
        return self.idle

    def clock(self):
        return self.now


async def no_analysis(win):
    return None


async def connect(core, scene=None, analyze=None, **kw):
    scene = scene or Scene()
    client = ModuleClient(build_manifest(), url=core.url, token=core.token)
    mod = DesktopModule(client, clock=scene.clock, idle_fn=scene.idle_fn, foreground_fn=scene.fg,
                        analyze_fn=analyze or no_analysis, **kw)
    task = asyncio.create_task(client.run())
    await core.wait_connected(timeout=10)
    return mod, scene, client, task


async def close(client, task):
    await client.stop()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 10)


@pytest.fixture
async def core():
    async with FakeCore(settings=dict(SETTINGS)) as c:
        yield c


def kinds(core):
    return [m.body["kind"] for m in core.messages_of("event")]


async def test_manifest_is_valid_and_every_tool_has_a_handler(core):
    mod, _scene, client, task = await connect(core)
    try:
        m = build_manifest()
        assert validate_manifest(m) == []
        names = {t["name"] for t in m["tools"]}
        assert names == set(client._tools)                       # обработчики есть у каждого инструмента
        assert {n for n in names if n in client._checks} == {"take_screenshot", "type_text", "press_key"}
        by = {t["name"]: t for t in m["tools"]}
        assert by["claude_task"]["job"] and by["claude_task"]["fallback"] and by["claude_task"]["core_fills"] == ["context"]
        assert {n for n, t in by.items() if t.get("announce")} == {"launch_app", "operate_screen", "claude_task", "menu_click"}
        assert m["capabilities"] == ["screen.observe"] and m["state"] is True
    finally:
        await close(client, task)


async def test_app_change_and_sensitive_window_hide_the_title(core):
    mod, scene, client, task = await connect(core)
    try:
        await mod._tick()                                        # notepad: стартовое приложение, без события
        scene.now += 4
        scene.win = {"title": "Почта - Chrome", "app": "chrome", "rect": (0, 0, 800, 600)}
        await mod._tick()
        scene.now += 6
        scene.win = {"title": "База паролей", "app": "keepass", "rect": (0, 0, 800, 600)}
        await mod._tick()
        await core.wait_message("event", lambda b: b["kind"] == "desktop.app_changed" and b["data"]["app"] == "keepass")
        changed = [m.body["data"] for m in core.messages_of("event") if m.body["kind"] == "desktop.app_changed"]
        assert changed[0] == {"app": "chrome", "title_hint": "Почта - Chrome"}
        assert changed[1] == {"app": "keepass", "title_hint": None}          # чувствительное: заголовка нет
        st = await core.wait_message("state", lambda b: b["data"].get("foreground_app") == "keepass")
        assert st.body["data"]["sensitive"] is True and st.body["data"]["idle_s"] == 0.0
    finally:
        await close(client, task)


async def test_idle_user_goes_away_and_comes_back(core):
    mod, scene, client, task = await connect(core)
    try:
        await mod._tick()
        scene.idle = 700.0
        scene.now += 2
        await mod._tick()
        scene.idle = 3.0
        scene.now += 2
        await mod._tick()
        await core.wait_message("event", lambda b: b["kind"] == "desktop.user_back")
        idle = [m.body for m in core.messages_of("event") if m.body["kind"] == "desktop.user_idle"]
        back = [m.body for m in core.messages_of("event") if m.body["kind"] == "desktop.user_back"]
        assert idle[0]["data"]["idle_s"] == 700 and back[0]["data"]["idle_s"] == 700
    finally:
        await close(client, task)


async def test_screen_is_not_analysed_when_window_is_sensitive(core):
    calls = []

    async def analyze(win):
        calls.append(win["app"])
        return {"summary": "пишет текст", "category": "docs"}

    mod, scene, client, task = await connect(core, analyze=analyze)
    try:
        scene.win = {"title": "База паролей", "app": "keepass", "rect": (0, 0, 800, 600)}
        await mod._tick()
        assert calls == []                                       # личное окно: ни снимка, ни анализа
        scene.now += 30
        scene.win = {"title": "Документ", "app": "notepad", "rect": (0, 0, 800, 600)}
        await mod._tick()
        ev = await core.wait_message("event", lambda b: b["kind"] == "desktop.activity")
        assert calls == ["notepad"] and ev.body["text"] == "notepad: пишет текст"
        assert ev.body["data"] == {"app": "notepad", "category": "docs", "summary": "пишет текст",
                                   "since": scene.now}
    finally:
        await close(client, task)


async def test_claude_task_is_a_job_asks_once_and_reports_done(core, monkeypatch):
    seen, usage = [], []

    async def fake_run(task, context, ask, usage_cb):
        seen.append((task, context))
        assert await ask("Хочу выполнить команду: ls.", None) is True
        await usage_cb(42, None)
        return "В проекте порт 8766."

    asked = []
    monkeypatch.setattr(appmod.assistant_agent, "run_task", fake_run)

    def answer_yes(body):
        asked.append(body)
        return {"answer": True}

    def record_usage(body):
        usage.append(body)
        return {}
    core.handlers["core.ask"] = answer_yes
    core.handlers["core.claude_usage"] = record_usage
    mod, _scene, client, task = await connect(core)
    try:
        value = await core.call_tool("claude_task", {"task": "найди порт", "context": "контекст"})
        assert re.fullmatch(r"j-[0-9a-f]{8}", value["job_id"])
        done = await core.wait_message("job.update", lambda b: b["job_id"] == value["job_id"] and b["state"] == "done")
        assert done.body["text"] == "В проекте порт 8766."
        assert seen == [("найди порт", "контекст")]
        assert len(asked) == 1 and asked[0]["kind"] == "approval"
        assert usage == [{"tokens": 42, "source": "desktop"}]
    finally:
        await close(client, task)


async def test_claude_task_without_answer_fails_with_error(core, monkeypatch):
    async def fake_run(task, context, ask, usage_cb):
        return None

    monkeypatch.setattr(appmod.assistant_agent, "run_task", fake_run)
    mod, _scene, client, task = await connect(core)
    try:
        value = await core.call_tool("claude_task", {"task": "x"})
        failed = await core.wait_message("job.update", lambda b: b["job_id"] == value["job_id"] and b["state"] == "failed")
        assert failed.body["error"]["code"] == "internal"
    finally:
        await close(client, task)


async def test_cancel_job_stops_the_run(core, monkeypatch):
    started = asyncio.Event()

    async def forever(task, context, ask, usage_cb):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(appmod.assistant_agent, "run_task", forever)
    mod, _scene, client, task = await connect(core)
    try:
        value = await core.call_tool("claude_task", {"task": "долго"})
        await asyncio.wait_for(started.wait(), 5)
        await core.job_cancel(value["job_id"])
        await core.wait_message("job.update", lambda b: b["job_id"] == value["job_id"] and b["state"] == "cancelled")
    finally:
        await close(client, task)


async def test_permission_is_not_asked_when_auto_accept_and_danger_always_is(monkeypatch):
    async with FakeCore(settings={**SETTINGS, "auto_accept": True}) as core:
        asked = []
        core.handlers["core.ask"] = lambda body: (asked.append(body), {"answer": False})[1]
        mod, _scene, client, task = await connect(core)
        try:
            assert await mod._ask("Хочу прочитать файл.", None) is True          # обычное при auto_accept: сразу да
            assert asked == []
            assert await mod._ask("Хочу выполнить rm.", "рекурсивное удаление") is False   # опасное: спросили
            assert len(asked) == 1 and "рекурсивное удаление" in asked[0]["question"]
        finally:
            await close(client, task)


async def test_permission_timeout_counts_as_no(core):
    core.handlers["core.ask"] = lambda body: {"answer": None}     # ядро ответило «по тайм-ауту»
    mod, _scene, client, task = await connect(core)
    try:
        assert await mod._ask("Хочу открыть страницу.", None) is False
    finally:
        await close(client, task)


async def test_fake_mode_refuses_screen_programs_and_claude(core):
    mod, _scene, client, task = await connect(core, fake=True)
    try:
        for name, args in [("take_screenshot", {}), ("describe_screen", {}), ("launch_app", {"name": "блокнот"}),
                           ("claude_task", {"task": "x"}), ("menu_click", {"text": "Продолжить"})]:
            with pytest.raises(ToolError) as err:
                await core.call_tool(name, args)
            assert err.value.code == "unavailable", name
        assert (await core.call_tool("last_screenshot")) == {"path": None}
    finally:
        await close(client, task)


async def test_menu_click_only_whitelisted_and_only_when_user_is_away(core, monkeypatch):
    clicked = []
    monkeypatch.setattr(pc_control, "_focus_window", lambda title: "ok")
    monkeypatch.setattr(pc_control, "_click_text", lambda text, button, double: clicked.append(text) or "нажала")
    mod, scene, client, task = await connect(core)
    try:
        with pytest.raises(ToolError) as denied:
            await core.call_tool("menu_click", {"text": "Удалить всё"})
        assert denied.value.code == "denied" and clicked == []
        scene.idle = 5.0
        with pytest.raises(ToolError) as busy:
            await core.call_tool("menu_click", {"text": "Продолжить"})
        assert busy.value.code == "unavailable" and clicked == []
        scene.idle = 300.0
        assert await core.call_tool("menu_click", {"text": "Продолжить"}) == "нажала"
        assert clicked == ["Продолжить"]
    finally:
        await close(client, task)


async def test_full_screen_shot_is_announced_as_fact_game_shot_is_not(core, monkeypatch, tmp_path):
    """Правило 14: снимок всего экрана объявляется (факт с announce); снимок окна игры - быстрая операция, не объявляется."""
    from PIL import Image
    shot = tmp_path / "shot.png"
    Image.new("RGB", (4, 4)).save(shot)
    monkeypatch.setattr(screenshots, "grab_screen", lambda: shot)
    monkeypatch.setattr(screenshots, "grab_game", lambda focus: shot)
    mod, scene, client, task = await connect(core)
    try:
        await core.call_tool("take_screenshot", {"target": "game"})
        assert [m.body for m in core.messages_of("say")] == []
        await core.call_tool("take_screenshot", {"target": "screen"})
        said = await core.wait_message("say", lambda b: b.get("announce") is True)
        assert said.body["mode"] == "fact" and "экран" in said.body["text"]
    finally:
        await close(client, task)


async def test_checks_flag_dangerous_keys_text_and_full_screen_shots(core):
    mod, _scene, client, task = await connect(core)
    try:
        assert await core.check_tool("press_key", {"keys": "alt+f4"})
        assert await core.check_tool("press_key", {"keys": "ctrl+s"}) is None
        assert await core.check_tool("type_text", {"text": "rm -rf /tmp/x"})
        assert await core.check_tool("type_text", {"text": "привет"}) is None
        assert await core.check_tool("take_screenshot", {"target": "screen"})
        assert await core.check_tool("take_screenshot", {"target": "game"}) is None
    finally:
        await close(client, task)


async def test_last_screenshot_returns_the_newest_file(core, tmp_path):
    folder = screenshots._dir()
    (folder / "shot_20260101_000000_game.png").write_bytes(b"x")
    mod, _scene, client, task = await connect(core)
    try:
        value = await core.call_tool("last_screenshot")
        assert value["path"].endswith("shot_20260101_000000_game.png")
    finally:
        await close(client, task)


async def test_state_is_sent_on_connect_with_foreground_app(core):
    mod, scene, client, task = await connect(core)
    try:
        await mod._tick()
        st = await core.wait_message("state", lambda b: b["data"].get("foreground_app") == "notepad")
        assert st.body["summary"].startswith("notepad")
        assert st.body["data"]["sensitive"] is False and st.body["data"]["activity"] is None
    finally:
        await close(client, task)
