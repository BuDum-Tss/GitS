"""Запуск RimWorld без настоящей игры: подмена процессов и порта."""
import subprocess

import pytest

import game_launcher as gl

_REAL_SPAWN = gl.spawn_detached        # до подмены conftest
_REAL_ANNOUNCE = gl.announce
_REAL_POST = gl._post_notify
REASON = "тест запуска ТЗ 11b"


@pytest.fixture
def spawn_allowed(monkeypatch):
    """Эти тесты проверяют сам запуск, но Popen в них подменён: настоящая игра не стартует. Остальным запуск запрещён conftest."""
    monkeypatch.delenv("GITS_NO_LAUNCH", raising=False)
    monkeypatch.setattr(gl, "spawn_detached", _REAL_SPAWN)


def test_launch_requires_a_reason():
    with pytest.raises(TypeError):
        gl.launch()
    with pytest.raises(ValueError, match="reason"):
        gl.launch("  ")


def test_user_is_told_before_the_game_is_spawned(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    order = []
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl, "announce", lambda reason: order.append(("announce", reason)))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: order.append(("spawn",)))
    gl.launch(REASON, wait_seconds=0)
    assert order == [("announce", REASON), ("spawn",)]


def test_forbidden_or_running_launch_is_not_announced(monkeypatch):
    monkeypatch.setattr(gl, "is_running", lambda: True)
    monkeypatch.setattr(gl, "mod_ready", lambda: True)
    gl.launch(REASON)
    assert gl.announced == []                                  # игра уже идёт: объявлять нечего


def test_announce_logs_and_prefers_the_module_notifier(monkeypatch):
    logged, told, posted = [], [], []
    monkeypatch.setattr(gl.events_log, "log", lambda kind, text="", **d: logged.append((kind, text)))
    monkeypatch.setattr(gl, "_post_notify", lambda text: posted.append(text) or True)
    gl.set_notifier(lambda text: told.append(text) or True)
    try:
        assert _REAL_ANNOUNCE("запись забега: игры не было") == "Ты сама запускаешь RimWorld (не пользователь): запись забега: игры не было"
    finally:
        gl.set_notifier(None)
    assert logged == [("game", "Ты сама запускаешь RimWorld (не пользователь): запись забега: игры не было")]
    assert told == ["Ты сама запускаешь RimWorld (не пользователь): запись забега: игры не было"] and posted == []
    gl.set_notifier(lambda text: False)                         # ядро через модуль недоступно: запасной путь по HTTP
    try:
        _REAL_ANNOUNCE("x")
    finally:
        gl.set_notifier(None)
    _REAL_ANNOUNCE("y")                                         # уведомителя нет (тесты, скрипты): HTTP
    assert posted == ["Ты сама запускаешь RimWorld (не пользователь): x", "Ты сама запускаешь RimWorld (не пользователь): y"]


def test_post_notify_sends_json_to_the_core_dashboard_and_ignores_a_dead_core(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.path, self.headers["Content-Type"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("GITS_NOTIFY_URL", f"http://127.0.0.1:{server.server_port}/api/notify")
        assert gl._post_notify("Ты сама запускаешь RimWorld (не пользователь): тест") is True
    finally:
        server.shutdown()
        server.server_close()
    assert got == [("/api/notify", "application/json", {"text": "Ты сама запускаешь RimWorld (не пользователь): тест", "source": "rimworld-tests"})]
    monkeypatch.setenv("GITS_NOTIFY_URL", "http://127.0.0.1:9/api/notify")        # порт закрыт
    assert gl._post_notify("x") is False


def test_launch_is_forbidden_by_env_and_never_reaches_popen(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    spawned = []
    monkeypatch.setenv("GITS_NO_LAUNCH", "1")
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    with pytest.raises(RuntimeError, match="GITS_NO_LAUNCH"):
        gl.launch(REASON, wait_seconds=0)
    assert spawned == [] and gl.announced == []                 # запрещённый запуск не объявляется


def test_already_running_does_not_start_second_copy(monkeypatch):
    monkeypatch.setattr(gl, "is_running", lambda: True)
    monkeypatch.setattr(gl, "mod_ready", lambda: True)
    started = []
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: started.append(a))
    assert "уже запущена" in gl.launch(REASON)
    assert started == []


def test_missing_exe_gives_clear_error(monkeypatch):
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: None)
    with pytest.raises(RuntimeError, match="GITS_RIMWORLD_EXE"):
        gl.launch(REASON)


def test_launch_waits_for_mod(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    state = {"up": False, "ticks": 0}
    monkeypatch.setattr(gl, "is_running", lambda: bool(state.get("started")))
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: state.update(started=True))

    def ready():
        state["ticks"] += 1
        return state["ticks"] >= 2
    monkeypatch.setattr(gl, "mod_ready", ready)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    assert "мод ответил" in gl.launch(REASON, wait_seconds=30)


def test_crash_at_start_is_reported(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(gl, "mod_ready", lambda: False)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="сразу закрылась"):
        gl.launch(REASON, wait_seconds=30)


def test_game_is_started_outside_the_bot_process_tree(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    calls = []
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    monkeypatch.setattr(gl.subprocess, "Popen", lambda args, **k: calls.append(args))
    monkeypatch.setattr(gl, "mod_ready", lambda: True)
    monkeypatch.setattr(gl.time, "sleep", lambda s: None)
    gl.launch(REASON, wait_seconds=10)
    assert calls[0][:3] == ["cmd.exe", "/c", "start"] and calls[0][-1] == str(exe)     # через start: игра не потомок бота


def test_launch_passes_args_to_game(spawn_allowed, monkeypatch, tmp_path):
    exe = tmp_path / "RimWorldWin64.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(gl, "is_running", lambda: False)
    monkeypatch.setattr(gl, "find_exe", lambda: str(exe))
    seen = []
    monkeypatch.setattr(gl.subprocess, "Popen", lambda cmd, **k: seen.append(cmd))
    gl.launch(REASON, wait_seconds=0, args=("-popupwindow",))
    assert seen[0][-1] == "-popupwindow" and seen[0][-2] == str(exe)


@pytest.mark.game
def test_real_launch_is_announced_and_game_starts(monkeypatch):
    """Настоящий запуск RimWorld (только `-All` или `-m game`): объявляется пользователю через ядро, игра закрывается сразу
    после проверки. Если игра уже запущена, тест её не трогает и пропускается."""
    import time
    if gl.is_running():
        pytest.skip("RimWorld уже запущена: тест не трогает чужую игру")
    if not gl.find_exe():
        pytest.skip("RimWorld не установлена")
    told = []
    gl.set_notifier(None)
    monkeypatch.setattr(gl, "_post_notify", lambda text: told.append(text) or _REAL_POST(text))
    monkeypatch.setattr(gl, "announce", _REAL_ANNOUNCE)
    try:
        gl.launch("тест настоящего запуска ТЗ 11b (игра закроется сразу)", wait_seconds=0)
        deadline = time.time() + 60
        while not gl.is_running() and time.time() < deadline:
            time.sleep(1)
        assert gl.is_running()
    finally:
        subprocess.run(["taskkill", "/IM", gl.PROCESS, "/F"], capture_output=True)       # закрыть то, что запустили сами
    assert told and told[0].startswith("Ты сама запускаешь RimWorld (не пользователь): тест настоящего запуска")


def test_launch_fact_says_the_character_launches_not_the_user():
    """DP-2: факт объявления не должен читаться как действие пользователя («Запускаю RimWorld: …»)."""
    text = _REAL_ANNOUNCE("запись забега: игры не было")
    assert text.startswith(gl.SELF_LAUNCH_PREFIX) and "не пользователь" in text and text.endswith("игры не было")
    assert not text.startswith("Запускаю")
