"""Хостовый помощник: сервер с подставным LocalHostBridge (токен, команды, состояние, скриншот, события, лимиты), прокси к
поддельному «моду» и RemoteHostBridge против настоящего сервера помощника в процессе. Игры, Windows-API и внешней сети нет."""
import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import host_bridge as hb
import server as srv

TOKEN = "test-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeBridge(hb.HostBridge):
    """Вместо LocalHostBridge: записывает вызовы, ничего не трогает."""

    def __init__(self):
        self.calls = []
        self.notifier = None
        self.size = (1920, 1080)
        self.fail_window = False
        self.record = None

    def set_notifier(self, fn):
        self.notifier = fn

    async def game_status(self):
        return {"running": True, "mod_ready": True, "window": "front", "user_idle_s": 5.0, "launch_forbidden": False}

    async def launch_game(self, reason, wait_seconds=0.0, windowed=True):
        self.calls.append(("launch", reason, wait_seconds, windowed))
        if reason == "падает":
            raise hb.HostBridgeError("игра запустилась и сразу закрылась", code="launch_failed")
        return "игра запущена"

    async def press_quick_start(self):
        self.calls.append(("quick_start",))
        return True

    async def show_window(self):
        self.calls.append(("show",))

    async def ui_screenshot(self):
        if self.fail_window:
            raise hb.HostBridgeError("Окно RimWorld не найдено", code="window_not_found")
        return b"\x89PNG-fake"

    async def ui_window_size(self):
        return self.size

    async def ui_click(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    async def ui_move(self, x, y):
        self.calls.append(("move", x, y))

    async def ui_drag(self, x1, y1, x2, y2, button="left"):
        self.calls.append(("drag", x1, y1, x2, y2, button))

    async def ui_type(self, text):
        self.calls.append(("type", text))

    async def ui_key(self, key):
        self.calls.append(("key", key))

    async def ui_scroll(self, x, y, amount):
        self.calls.append(("scroll", x, y, amount))

    async def record_start(self, run_id, colony, why=""):
        self.calls.append(("rec_start", run_id, colony, why))
        self.record = {"id": "rec1", "run_id": run_id, "colony": colony, "raw": "C:/v/raw.mp4", "started": time.time(),
                       "video_alive": True, "audio_present": True, "audio_ok": True, "audio_stopping": False,
                       "audio_error": "", "has_sound": False}
        return self.record

    async def record_status(self):
        return self.record

    async def record_ensure_alive(self, max_hours=None):
        self.calls.append(("ensure_alive", max_hours))
        return {"closed_id": None, "status": self.record}

    async def record_ensure_audio(self):
        return True

    async def record_finish(self, record_id, outcome, mode, markers=None, goal=None, events=None):
        self.calls.append(("finish", record_id, outcome, mode, markers, goal, events))
        return {"run_id": "r1", "mode": mode}


@pytest.fixture
async def helper():
    """(клиент, мост, журнал): настоящий сервер на случайном порту."""
    bridge = FakeBridge()
    journal = srv.EventJournal(keep=5)
    app = srv.build_app(bridge, TOKEN, journal=journal, wire=True, mirror_log=False)
    server = TestServer(app)
    await server.start_server()
    yield server, bridge, journal
    await server.close()


async def http(server, method, path, body=None, headers=AUTH, raw=False):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.request(method, str(server.make_url(path)), json=body, headers=headers) as r:
            data = await r.read()
            if raw:
                return r.status, data
            return r.status, (json.loads(data) if data else None)


# ── токен ──

async def test_token_is_required_everywhere_but_health(helper):
    server, _, _ = helper
    assert (await http(server, "GET", "/health", headers={}))[0] == 200
    for headers in ({}, {"Authorization": "Bearer не-тот"}, {"Authorization": TOKEN}, {"Authorization": "Basic xxx"}):
        for method, path in (("GET", "/game/status"), ("POST", "/game/launch"), ("GET", "/events"), ("GET", "/ui/screenshot"),
                             ("GET", "/game-api/ping")):
            status, body = await http(server, method, path, {} if method == "POST" else None, headers=headers)
            assert status == 401 and body["code"] == "unauthorized"


def test_server_refuses_to_start_without_token():
    with pytest.raises(ValueError):
        srv.build_app(FakeBridge(), "")


async def test_token_never_appears_in_error_bodies(helper):
    server, _, _ = helper
    status, body = await http(server, "POST", "/game/launch", {"reason": ""})
    assert status == 400 and TOKEN not in json.dumps(body, ensure_ascii=False)


# ── команды и состояние ──

async def test_status_and_launch(helper):
    server, bridge, _ = helper
    status, body = await http(server, "GET", "/game/status")
    assert status == 200 and body["running"] is True and body["window"] == "front" and body["recording"] is None
    status, body = await http(server, "POST", "/game/launch", {"reason": "тест", "wait_seconds": 3})
    assert status == 200 and body == {"result": "игра запущена"} and bridge.calls[-1] == ("launch", "тест", 3.0, True)
    assert (await http(server, "POST", "/game/launch", {"reason": "падает"}))[0] == 409
    assert (await http(server, "POST", "/game/launch", {"wait_seconds": 1}))[0] == 400            # нет причины
    assert (await http(server, "POST", "/game/launch", {"reason": "x", "wait_seconds": 9999}))[0] == 400


async def test_quick_start_and_show_window(helper):
    server, bridge, _ = helper
    assert await http(server, "POST", "/game/quick_start", {}) == (200, {"pressed": True})
    assert (await http(server, "POST", "/game/show_window", {}))[0] == 200 and bridge.calls[-1] == ("show",)


async def test_screenshot_is_binary_and_missing_window_is_409(helper):
    server, bridge, _ = helper
    status, data = await http(server, "GET", "/ui/screenshot", raw=True)
    assert status == 200 and data == b"\x89PNG-fake"
    bridge.fail_window = True
    status, body = await http(server, "GET", "/ui/screenshot")
    assert status == 409 and body["code"] == "window_not_found"
    assert await http(server, "GET", "/ui/window") == (200, {"width": 1920, "height": 1080})


async def test_ui_actions_only_inside_the_game_window(helper):
    server, bridge, _ = helper
    assert (await http(server, "POST", "/ui/click", {"x": 10, "y": 20, "button": "right", "clicks": 2}))[0] == 200
    assert bridge.calls[-1] == ("click", 10, 20, "right", 2)
    assert (await http(server, "POST", "/ui/click", {"x": 1920, "y": 5}))[0] == 400            # правее окна
    assert (await http(server, "POST", "/ui/click", {"x": -1, "y": 5}))[0] == 400
    assert (await http(server, "POST", "/ui/click", {"x": "1", "y": 5}))[0] == 400
    assert (await http(server, "POST", "/ui/click", {"x": 1, "y": 5, "button": "x"}))[0] == 400
    assert (await http(server, "POST", "/ui/drag", {"x1": 1, "y1": 2, "x2": 3000, "y2": 4}))[0] == 400
    assert (await http(server, "POST", "/ui/drag", {"x1": 1, "y1": 2, "x2": 30, "y2": 40}))[0] == 200
    assert (await http(server, "POST", "/ui/move", {"x": 5, "y": 6}))[0] == 200
    assert (await http(server, "POST", "/ui/scroll", {"x": 5, "y": 6, "amount": -3}))[0] == 200
    assert (await http(server, "POST", "/ui/scroll", {"x": 5, "y": 6, "amount": 5000}))[0] == 400


async def test_keys_and_text_are_limited(helper):
    server, bridge, _ = helper
    assert (await http(server, "POST", "/ui/key", {"key": "Enter"}))[0] == 200 and bridge.calls[-1] == ("key", "enter")
    assert (await http(server, "POST", "/ui/key", {"key": "w"}))[0] == 200
    for bad in ("win", "winleft", "ctrl+alt+delete", "enter; calc"):
        assert (await http(server, "POST", "/ui/key", {"key": bad}))[0] == 400
    assert (await http(server, "POST", "/ui/type", {"text": "Колония"}))[0] == 200
    assert (await http(server, "POST", "/ui/type", {"text": "я" * 501}))[0] == 400


async def test_request_body_limits(helper):
    server, _, _ = helper
    status, body = await http(server, "POST", "/ui/type", {"text": "x" * (srv.JSON_LIMIT + 10)})
    assert status == 413 and body["code"] == "too_large"
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.post(str(server.make_url("/ui/type")), data=b"not json", headers=AUTH) as r:
            assert r.status == 400


async def test_record_endpoints(helper):
    server, bridge, journal = helper
    status, body = await http(server, "POST", "/record/start", {"run_id": "r1", "colony": "Колония", "why": "забег"})
    assert status == 200 and body["status"]["id"] == "rec1"
    assert (await http(server, "GET", "/record/status"))[1]["status"]["run_id"] == "r1"
    status, body = await http(server, "POST", "/record/ensure_alive", {"max_hours": 6})
    assert status == 200 and body["closed_id"] is None and bridge.calls[-1] == ("ensure_alive", 6)
    assert (await http(server, "POST", "/record/ensure_audio", {}))[1] == {"restarted": True}
    status, body = await http(server, "POST", "/record/finish", {"id": "rec1", "outcome": "colony_died", "mode": "handoff",
                                                                 "markers": [[1.0, "метка", 3]], "events": [{"ts": 1}]})
    assert status == 200 and body["result"]["mode"] == "handoff"
    assert bridge.calls[-1] == ("finish", "rec1", "colony_died", "handoff", [[1.0, "метка", 3]], None, [{"ts": 1}])
    assert (await http(server, "POST", "/record/finish", {"mode": "бред"}))[0] == 400
    assert (await http(server, "POST", "/record/finish", {"mode": "stop", "markers": "x"}))[0] == 400
    assert (await http(server, "POST", "/record/start", {"run_id": "", "colony": "К"}))[0] == 400
    assert [e["data"]["op"] for e in journal.after(0)] == ["start", "audio_restarted", "finish"]


# ── события ──

async def test_events_numbers_are_monotonic_and_journal_is_bounded(helper):
    server, bridge, journal = helper
    for i in range(8):
        bridge.notifier(f"факт {i}")                                   # так объявления доходят из game_launcher
    status, body = await http(server, "GET", "/events?after=0&wait=0")
    assert status == 200 and [e["n"] for e in body["events"]] == [4, 5, 6, 7, 8]      # журнал хранит 5 последних
    assert body["last"] == 8 and body["first"] == 4 and body["epoch"] == journal.epoch
    assert body["events"][0]["type"] == "announce" and body["events"][-1]["data"] == {"text": "факт 7"}
    status, body = await http(server, "GET", "/events?after=7&wait=0")
    assert [e["n"] for e in body["events"]] == [8]
    assert (await http(server, "GET", "/events?after=x"))[0] == 400


async def test_events_long_poll_wakes_on_new_event(helper):
    server, bridge, _ = helper
    started = time.monotonic()
    task = asyncio.create_task(http(server, "GET", "/events?after=0&wait=10"))
    await asyncio.sleep(0.3)
    assert not task.done()
    await asyncio.to_thread(bridge.notifier, "из потока")              # объявления идут из рабочих потоков
    status, body = await asyncio.wait_for(task, 5)
    assert status == 200 and body["events"][0]["data"]["text"] == "из потока" and time.monotonic() - started < 5
    started = time.monotonic()
    status, body = await http(server, "GET", "/events?after=1&wait=0.4")      # новых нет: пусто по истечении ожидания
    assert body["events"] == [] and 0.3 < time.monotonic() - started < 3


async def test_events_wait_is_capped():
    assert srv.EVENTS_MAX_WAIT_S == 25.0


# ── прокси к моду ──

class FakeMod:
    def __init__(self):
        self.requests = []
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.server = TestServer(app, host="127.0.0.1")

    async def handle(self, request):
        body = await request.read()
        self.requests.append((request.method, request.path_qs, body, request.headers.get("Host"),
                              request.headers.get("Authorization")))
        if request.path == "/ping":
            return web.json_response({"status": "ok"})
        if request.path == "/boom":
            return web.json_response({"status": "error", "message": "нет"}, status=500)
        if request.path == "/missing":
            return web.Response(status=404, text="нет такого")
        if request.path == "/slow":
            await asyncio.sleep(5)
        if request.path == "/big":
            return web.Response(body=b"x" * (srv.PROXY_RESPONSE_LIMIT + 10))
        return web.json_response({"echo": body.decode("utf-8") if body else None, "q": request.query_string})


@pytest.fixture
async def proxied(monkeypatch):
    mod = FakeMod()
    await mod.server.start_server()
    app = srv.build_app(FakeBridge(), TOKEN, rimworld_api=f"http://127.0.0.1:{mod.server.port}")
    helper = TestServer(app)
    await helper.start_server()
    yield helper, mod
    await helper.close()
    await mod.server.close()


async def test_proxy_get_post_passes_query_body_and_status(proxied):
    helper, mod = proxied
    assert await http(helper, "GET", "/game-api/ping") == (200, {"status": "ok"})
    status, body = await http(helper, "GET", "/game-api/state?full=1&x=а")
    assert status == 200 and body["q"] == "full=1&x=а"
    status, body = await http(helper, "POST", "/game-api/command", {"type": "set_time_speed", "speed": 1})
    assert status == 200 and json.loads(body["echo"]) == {"type": "set_time_speed", "speed": 1}
    method, path, _, host, auth = mod.requests[-1]
    assert method == "POST" and path == "/command"
    assert host == f"localhost:{mod.server.port}" and auth is None             # мод принимает только имя localhost; токен до него не доходит


async def test_proxy_returns_mod_errors_as_is(proxied):
    helper, _ = proxied
    status, body = await http(helper, "GET", "/game-api/boom")
    assert status == 500 and body == {"status": "error", "message": "нет"}
    status, data = await http(helper, "GET", "/game-api/missing", raw=True)
    assert status == 404 and data.decode("utf-8") == "нет такого"


async def test_proxy_requires_token_and_valid_path(proxied):
    helper, mod = proxied
    assert (await http(helper, "GET", "/game-api/ping", headers={}))[0] == 401
    assert (await http(helper, "GET", "/game-api/", ))[0] == 400
    assert (await http(helper, "GET", "/game-api/../health"))[0] in (400, 200, 404)
    assert (await http(helper, "PUT", "/game-api/command", {}))[0] == 405
    assert mod.requests == [] or all(r[1] != "/../health" for r in mod.requests)


async def test_proxy_limits(proxied, monkeypatch):
    helper, _ = proxied
    status, body = await http(helper, "POST", "/game-api/command", {"x": "y" * (srv.PROXY_BODY_LIMIT + 10)})
    assert status == 413
    monkeypatch.setattr(srv, "PROXY_RESPONSE_LIMIT", 100)
    status, body = await http(helper, "GET", "/game-api/big")
    assert status == 502 and body["code"] == "mod_response_too_large"


async def test_proxy_timeout_and_unreachable_mod(proxied, monkeypatch):
    helper, mod = proxied
    monkeypatch.setattr(srv, "PROXY_TIMEOUT_S", 0.3)
    status, body = await http(helper, "GET", "/game-api/slow")
    assert status == 504 and body["code"] == "mod_timeout"
    monkeypatch.setattr(srv, "PROXY_TIMEOUT_S", 15.0)                   # отказ в соединении в Windows занимает пару секунд
    with socket.socket() as s:                                          # закрытый порт: мода нет
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    app = srv.build_app(FakeBridge(), TOKEN, rimworld_api=f"http://127.0.0.1:{dead}")
    server = TestServer(app)
    await server.start_server()
    try:
        status, body = await http(server, "GET", "/game-api/ping")
        assert status == 502 and body["code"] == "mod_unreachable"
    finally:
        await server.close()


async def test_proxy_falls_back_to_ipv6_loopback_for_default_target():
    app = srv.HostApp(FakeBridge(), TOKEN)
    assert app.mod_targets == [srv.MOD_DEFAULT, srv.MOD_FALLBACK]
    assert srv.HostApp(FakeBridge(), TOKEN, rimworld_api="http://127.0.0.1:1").mod_targets == ["http://127.0.0.1:1"]


# ── RemoteHostBridge против настоящего сервера помощника ──

@pytest.fixture
def remote_env(monkeypatch):
    import game_launcher
    yield
    game_launcher.set_notifier(None)


async def test_remote_commands_and_status(helper, remote_env):
    server, fake, _ = helper
    bridge = hb.RemoteHostBridge(str(server.make_url("")), TOKEN)
    status = await bridge.game_status()
    assert status["running"] and status["window"] == "front" and status["user_idle_s"] == 5.0
    assert await bridge.launch_game("тест", 2.0, windowed=True) == "игра запущена"
    assert fake.calls[-1] == ("launch", "тест", 2.0, True)
    assert await bridge.press_quick_start() is True
    await bridge.show_window()
    assert await bridge.ui_screenshot() == b"\x89PNG-fake" and await bridge.ui_window_size() == (1920, 1080)
    await bridge.ui_click(3, 4, "right", 2)
    await bridge.ui_key("enter")
    await bridge.ui_scroll(1, 2, -3)
    assert fake.calls[-3:] == [("click", 3, 4, "right", 2), ("key", "enter"), ("scroll", 1, 2, -3)]


async def test_remote_errors_are_clear(helper, remote_env):
    server, fake, _ = helper
    url = str(server.make_url(""))
    with pytest.raises(hb.HostBridgeError) as e:
        await hb.RemoteHostBridge(url, "чужой-токен").game_status()
    assert e.value.code == "unauthorized" and "GITS_RIMWORLD_HOST_TOKEN" in str(e.value) and "чужой-токен" not in str(e.value)
    bridge = hb.RemoteHostBridge(url, TOKEN)
    with pytest.raises(hb.HostBridgeError) as e:
        await bridge.launch_game("падает")
    assert e.value.code == "launch_failed" and e.value.status == 409 and "закрылась" in str(e.value)
    fake.fail_window = True
    with pytest.raises(hb.HostBridgeError) as e:
        await bridge.ui_screenshot()
    assert e.value.code == "window_not_found"
    with pytest.raises(hb.HostBridgeError) as e:
        await bridge.ui_click(5000, 5)
    assert e.value.code == "bad_request"


async def test_remote_unavailable_helper_gives_clear_error_not_crash(remote_env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = s.getsockname()[1]
    bridge = hb.RemoteHostBridge(f"http://127.0.0.1:{dead}", TOKEN, timeout=2, events_wait=0.2)
    with pytest.raises(hb.HostUnavailable) as e:
        await bridge.game_status()
    assert "недоступен" in str(e.value) and bridge.connected is False and TOKEN not in str(e.value)
    bridge.start_events()                                               # цикл опроса при сбое связи живёт и ждёт
    await asyncio.sleep(0.5)
    assert not bridge._poller.done()
    await bridge.close()
    assert bridge._poller is None


async def test_remote_events_reach_the_same_handlers(helper, remote_env, monkeypatch):
    server, fake, journal = helper
    import events_log
    logged, told, rec_events = [], [], []
    monkeypatch.setattr(events_log, "log", lambda kind, text="", **d: logged.append((kind, text, d)))
    bridge = hb.RemoteHostBridge(str(server.make_url("")), TOKEN, events_wait=1.0)
    bridge.set_notifier(lambda text: told.append(text) or True)
    bridge.on_record_event = rec_events.append
    journal.add("announce", {"text": "старый факт"})                    # было до подключения: не повторяется
    bridge.start_events()
    await asyncio.sleep(0.3)
    fake.notifier("Ты сама запускаешь RimWorld")
    journal.add("log", {"kind": "video", "text": "запись звука оборвалась", "data": {"parts": 2}})
    await http(server, "POST", "/record/start", {"run_id": "r1", "colony": "К"})
    for _ in range(50):
        if told and logged and rec_events:
            break
        await asyncio.sleep(0.1)
    await bridge.close()
    assert told == ["Ты сама запускаешь RimWorld"]
    assert logged == [("video", "запись звука оборвалась", {"parts": 2})]
    assert rec_events and rec_events[0]["op"] == "start"


async def test_remote_poller_survives_handler_failure_and_helper_restart(helper, remote_env):
    server, fake, journal = helper
    bridge = hb.RemoteHostBridge(str(server.make_url("")), TOKEN, events_wait=0.5)
    got = []

    def notifier(text):
        got.append(text)
        if text == "падаю":
            raise RuntimeError("обработчик сломан")
        return True
    bridge.set_notifier(notifier)
    bridge.start_events()
    await asyncio.sleep(0.3)
    journal.add("announce", {"text": "падаю"})
    journal.add("announce", {"text": "живу"})
    for _ in range(40):
        if "живу" in got:
            break
        await asyncio.sleep(0.1)
    await bridge.close()
    assert got == ["падаю", "живу"]


async def test_remote_recorder_mirrors_host_and_hands_off(helper, remote_env, tmp_path):
    """Контейнерная запись: зеркало состояния, метки и события уходят хосту при закрытии."""
    server, fake, _ = helper
    import events_log
    events_log.init(str(tmp_path))
    bridge = hb.RemoteHostBridge(str(server.make_url("")), TOKEN)
    recorder = bridge.make_recorder(str(tmp_path), lambda: {"record_runs": True})
    recorder.start("r1", "Колония", why="забег")
    for _ in range(50):
        if recorder.current is not None and getattr(recorder.current, "remote_id", None):
            break
        await asyncio.sleep(0.1)
    rec = recorder.current
    assert rec.remote_id == "rec1" and rec.raw == Path("C:/v/raw.mp4") and rec.proc.poll() is None
    assert rec.audio.ok and fake.calls[-1] == ("rec_start", "r1", "Колония", "забег")
    recorder.mark("налёт", 2)
    fake.record["video_alive"] = False                                   # ffmpeg на хосте умер
    await asyncio.to_thread(recorder.refresh)
    assert rec.proc.poll() == 1
    fake.record["video_alive"], fake.record["audio_ok"] = True, False
    await asyncio.to_thread(recorder.refresh)
    assert rec.proc.poll() is None and rec.audio.ok is False
    assert await asyncio.to_thread(recorder.ensure_audio) is True
    assert await asyncio.to_thread(recorder.ensure_alive) is None        # хост ничего не закрыл
    closed = recorder.close("colony_died")
    assert recorder.current is None
    result = await recorder.produce_for_montage(closed)
    assert result == {"run_id": "r1", "mode": "handoff"}
    call = fake.calls[-1]
    assert call[:4] == ("finish", "rec1", "colony_died", "handoff") and call[4][0][1:] == ["налёт", 2] and call[6] == []
    await asyncio.to_thread(recorder.stop_blocking, closed)
    assert fake.calls[-1][3] == "stop"


async def test_remote_recorder_start_failure_clears_mirror(helper, remote_env, tmp_path):
    server, fake, _ = helper
    bridge = hb.RemoteHostBridge(str(server.make_url("")), "чужой")
    recorder = bridge.make_recorder(str(tmp_path), lambda: {"record_runs": True})
    recorder.start("r1", "К")
    await asyncio.wait({recorder.current.start_task}) if recorder.current else None
    assert recorder.current is None
    assert (await recorder.produce_for_montage(type("R", (), {"remote_id": None, "start_task": None})())) is None


async def test_module_reaches_mod_through_helper_with_token(proxied, monkeypatch):
    """Контейнерный режим: RIMWORLD_API=<помощник>/game-api, токен в заголовке; контроллер и gamenet работают как с самим модом."""
    helper, mod = proxied
    import controller
    import gamenet
    base = str(helper.make_url("/game-api")).rstrip("/")
    monkeypatch.setenv("RIMWORLD_API", base)
    monkeypatch.setenv("GITS_RIMWORLD_HOST_TOKEN", TOKEN)
    monkeypatch.setattr(gamenet, "_LOCAL_NAMES", ("",))                 # тестовый помощник на 127.0.0.1 считаем «чужим» хостом
    monkeypatch.setattr(controller, "_BASE", base)
    assert await asyncio.to_thread(gamenet.port_open) is True           # «игра запущена» = /ping через прокси
    assert await asyncio.to_thread(controller._ping) is True
    assert await asyncio.to_thread(controller._game_command, {"type": "set_time_speed", "speed": 1}) is True
    assert mod.requests[-1][:2] == ("POST", "/command")
    monkeypatch.setenv("GITS_RIMWORLD_HOST_TOKEN", "не тот")             # токен отклонён: модуль видит «игры нет», а не падает
    assert await asyncio.to_thread(gamenet.port_open) is False
    assert await asyncio.to_thread(controller._ping) is False
