"""Инструмент restart_game: перезапуск RimWorld целиком (сохранить, закрыть мягко или жёстко, проверить, запустить заново).

Игры, процессов и помощника нет: мост подставной (`FakeHost`), сохранение и состояние игры подменены. Согласование с ядром:
пока игра перезапускается, среда не считается завершённой (`done` остаётся false до `launch_wait_s`)."""
import json

import httpx
import pytest
from gits_sdk import ToolError

import controller
import focus_checkpoint
import focus_mode
import game_launcher
import game_modes
import host_bridge as hb
import module_manifest
import planning_pause
import policy_manager
import watch_rules


class FakeHost(hb.HostBridge):
    """Мост без игры: записывает порядок вызовов; `status` меняется по ходу (игра «закрывается» после stop_game)."""

    def __init__(self, running=True, mod_ready=True, how="graceful", forbidden=False, launch_error=None, stays_alive=False):
        self.running, self.mod, self.how, self.forbidden = running, mod_ready, how, forbidden
        self.launch_error, self.stays_alive = launch_error, stays_alive
        self.calls: list[tuple] = []
        self.on_stop = None                                  # хук: что происходит в момент закрытия (тест тикает контроллер)

    def announce_self(self, fact, kind="game"):
        self.calls.append(("announce", fact))
        return fact

    async def game_status(self):
        return {"running": self.running, "mod_ready": self.mod and self.running, "window": "front", "user_idle_s": 0.0,
                "launch_forbidden": self.forbidden}

    async def stop_game(self, graceful_s=10.0):
        self.calls.append(("stop", graceful_s))
        if self.on_stop:
            await self.on_stop()
        if not self.stays_alive:
            self.running = False
        return {"stopped": True, "how": self.how}

    async def launch_game(self, reason, wait_seconds=0.0, windowed=True):
        self.calls.append(("launch", reason, wait_seconds, windowed))
        if self.launch_error:
            raise hb.HostBridgeError(self.launch_error, code="launch_failed")
        self.running = True
        return "игра запущена"

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    monkeypatch.setattr(watch_rules, "FILE", tmp_path / "watch.json")
    monkeypatch.setattr(focus_checkpoint, "FILE", tmp_path / "checkpoint.json")
    monkeypatch.setattr(policy_manager, "REQUEST", tmp_path / "policy_request.json")
    monkeypatch.setattr(game_modes, "_REQUEST", tmp_path / "mode_request.json")
    monkeypatch.setattr(planning_pause, "_MARK", tmp_path / "planner_paused.json")
    monkeypatch.setattr(controller, "MEM_DIR", tmp_path)
    (tmp_path / "goals.json").write_text(json.dumps(
        [{"id": "g1", "category": "ambition", "status": "active", "title": "Выжить на берегу"}]), encoding="utf-8")
    monkeypatch.setattr(controller, "_game_command", lambda cmd: True)
    monkeypatch.setattr(controller, "_ping", lambda: False)
    monkeypatch.setattr(controller, "_get_events", lambda: [])
    monkeypatch.setattr(controller, "_get_state", lambda: {})
    monkeypatch.setattr(controller, "RESTART_GONE_WAIT_S", 0.2)
    host = FakeHost()
    c = controller.Controller({"memory_dir": str(tmp_path), "host_bridge": host})
    c._ensure_run_started = lambda state: None
    c.fake_host = host
    c.logged = []
    monkeypatch.setattr(controller.events_log, "log", lambda kind, text="", **d: c.logged.append((kind, text, d)))
    c.saves = []
    monkeypatch.setattr(controller, "_save_game", lambda name, timeout: c.saves.append((name, timeout)) or (True, name))
    return c


def use_core_focus(ctrl, monkeypatch):
    monkeypatch.setattr(ctrl, "_module_setting", lambda key, default: "core" if key == "focus_mode" else default)


def game_state(monkeypatch, up: bool):
    state = {"tick": 1000, "paused": False, "save_id": "s1", "threats": [],
             "colony": {"pawns": [], "resources": {}, "buildings": [], "pending_construction": []}}
    monkeypatch.setattr(controller, "_get_state", lambda: state if up else {})


async def _ticks(ctrl, n=None):
    for _ in range(n or focus_mode.GAME_GONE_TICKS + 2):
        await ctrl._tick()


# ── последовательность ───────────────────────────────────────────────────────────────────────────

async def test_sequence_announce_save_stop_launch(ctrl):
    text = await ctrl.restart_game("зависла игра: тик стоит")
    host = ctrl.fake_host
    assert host.names() == ["announce", "stop", "launch"]
    assert ctrl.saves == [(controller.RESTART_SAVE_NAME, controller.RESTART_SAVE_TIMEOUT_S)]
    assert "зависла игра" in host.calls[0][1] and host.calls[0][1].startswith("перезапускаю RimWorld")
    assert host.calls[1] == ("stop", controller.RESTART_STOP_GRACEFUL_S)
    assert host.calls[2][2] == 0.0 and host.calls[2][3] is True            # не ждём загрузку, окно без рамки
    assert "сохранение да" in text and "закрыта: мягко" in text and "запуск начат" in text and "№1" in text
    kinds = [t for k, t, d in ctrl.logged if k == "game"]
    assert any("перезапуск игры №1" in t for t in kinds) and any(d.get("restart") == 1 for _, _, d in ctrl.logged)


async def test_graceful_close_failed_then_killed(ctrl):
    ctrl.fake_host.how = "killed"
    text = await ctrl.restart_game("мод не работает")
    assert "закрыта: жёстко" in text and ctrl.fake_host.names()[-1] == "launch"


async def test_game_was_not_running_skips_save_and_stop(ctrl):
    ctrl.fake_host.running = False
    text = await ctrl.restart_game("игры нет")
    assert ctrl.fake_host.names() == ["announce", "launch"] and ctrl.saves == []
    assert "игра не была запущена" in text and "сохранение нет" in text


async def test_save_first_false_does_not_save(ctrl):
    text = await ctrl.restart_game("ошибки текстур", save_first=False)
    assert ctrl.saves == [] and "сохранение нет" in text and ctrl.fake_host.names() == ["announce", "stop", "launch"]


async def test_mod_without_save_command_continues_with_warning(ctrl, monkeypatch):
    monkeypatch.setattr(controller, "_save_game", lambda name, timeout: (False, "мод без команды сохранения: нужна пересборка мода"))
    text = await ctrl.restart_game("зависла")
    assert ctrl.fake_host.names() == ["announce", "stop", "launch"]
    assert "сохранение нет" in text and "мод без команды сохранения" in text and "без сохранения" in text


async def test_hung_game_that_does_not_answer_is_restarted_without_save(ctrl):
    ctrl.fake_host.mod = False                                              # процесс жив, мод молчит (зависание)
    text = await ctrl.restart_game("зависла")
    assert ctrl.saves == [] and "stop" in ctrl.fake_host.names() and "мод не отвечает" in text


# ── лимит, запреты, сбои ─────────────────────────────────────────────────────────────────────────

async def test_rate_limit_120_seconds(ctrl):
    await ctrl.restart_game("первый")
    ctrl.fake_host.calls.clear()
    with pytest.raises(RuntimeError, match="не чаще раза в 120 с"):
        await ctrl.restart_game("второй")
    assert ctrl.fake_host.calls == []                                       # отказ до любых действий с игрой
    ctrl._last_restart_at -= controller.RESTART_MIN_INTERVAL_S + 1
    assert "№2" in await ctrl.restart_game("третий")
    assert ctrl._restart_count == 2


async def test_reason_is_required(ctrl):
    with pytest.raises(ValueError, match="reason"):
        await ctrl.restart_game("  ")
    assert ctrl.fake_host.calls == [] and ctrl._last_restart_at is None


async def test_launch_forbidden_does_not_touch_the_game(ctrl):
    ctrl.fake_host.forbidden = True
    with pytest.raises(RuntimeError, match="GITS_NO_LAUNCH=1"):
        await ctrl.restart_game("зависла")
    assert ctrl.fake_host.calls == [] and ctrl.saves == [] and ctrl._last_restart_at is None and ctrl._restart_count == 0


async def test_local_bridge_respects_gits_no_launch(ctrl, monkeypatch):
    """Настоящий LocalHostBridge: GITS_NO_LAUNCH=1 (стоит в тестах) запрещает перезапуск ещё до закрытия игры."""
    monkeypatch.setattr(game_launcher, "is_running", lambda: True)
    monkeypatch.setattr(game_launcher, "mod_ready", lambda: True)
    monkeypatch.setattr("game_watchdog.window_state", lambda: "front")
    monkeypatch.setattr("game_watchdog.user_idle_seconds", lambda: 0.0)

    def boom(*a, **k):
        raise AssertionError("игру нельзя закрывать при GITS_NO_LAUNCH=1")
    monkeypatch.setattr(game_launcher, "stop", boom)
    ctrl.host = hb.LocalHostBridge()
    with pytest.raises(RuntimeError, match="GITS_NO_LAUNCH=1"):
        await ctrl.restart_game("зависла")


async def test_game_that_stays_alive_is_not_launched_again(ctrl):
    ctrl.fake_host.stays_alive = True
    with pytest.raises(RuntimeError, match="не закрылась"):
        await ctrl.restart_game("зависла")
    assert "launch" not in ctrl.fake_host.names() and ctrl._restart_busy is False


async def test_launch_failure_is_reported_after_close(ctrl):
    ctrl.fake_host.launch_error = "не нашла RimWorldWin64.exe"
    with pytest.raises(RuntimeError, match="игра закрыта, но запуск не вышел"):
        await ctrl.restart_game("зависла")
    assert ctrl._restart_busy is False


async def test_concurrent_restart_is_refused(ctrl):
    ctrl._restart_busy = True
    with pytest.raises(RuntimeError, match="уже идёт"):
        await ctrl.restart_game("ещё раз")


# ── согласование с ядром: done остаётся false во время перезапуска ──────────────────────────────────

async def test_done_stays_false_while_the_game_restarts(ctrl, monkeypatch):
    use_core_focus(ctrl, monkeypatch)
    game_state(monkeypatch, True)
    monkeypatch.setattr(controller, "_ping", lambda: True)
    assert (await ctrl.activate({"goal": "играть"}))["ok"]
    await _ticks(ctrl, 2)
    assert ctrl._lease_game_seen and ctrl.policy_state()["status"]["done"] is False
    seen = []

    async def closing():                                   # игра закрывается, пока идёт перезапуск
        game_state(monkeypatch, False)
        await _ticks(ctrl)                                 # мод молчит дольше GAME_GONE_TICKS
        seen.append(ctrl.policy_state()["status"]["done"])
    ctrl.fake_host.on_stop = closing
    await ctrl.restart_game("зависла")
    assert seen == [False] and ctrl.policies.in_focus and ctrl._focus_source == "core"
    assert ctrl._lease_game_seen is False and ctrl._launch_wait_since is not None   # как у launch_game
    await _ticks(ctrl)                                     # игра грузится, мод ещё не отвечает
    st = ctrl.policy_state()["status"]
    assert st["done"] is False and "ждём запуск игры" in st["active_goal"]
    game_state(monkeypatch, True)                          # новая игра ответила
    await _ticks(ctrl, 2)
    assert ctrl._lease_game_seen and ctrl.policy_state()["status"]["done"] is False


async def test_done_after_launch_wait_timeout_if_game_never_comes(ctrl, monkeypatch):
    use_core_focus(ctrl, monkeypatch)
    game_state(monkeypatch, True)
    await ctrl.activate({"goal": "играть"})
    await _ticks(ctrl, 2)
    game_state(monkeypatch, False)
    monkeypatch.setattr(ctrl, "_module_setting", lambda key, default: 60 if key == "launch_wait_s" else "core" if key == "focus_mode" else default)
    await ctrl.restart_game("зависла")
    await _ticks(ctrl)
    assert ctrl.policy_state()["status"]["done"] is False
    ctrl._launch_wait_since -= 61
    await _ticks(ctrl, 1)
    assert ctrl.policy_state()["status"]["done"] is True and "launch_wait_s" in ctrl.policies.reason


async def test_watchdog_does_not_launch_during_restart(ctrl):
    ctrl._restart_busy = True
    assert ctrl._launch_allowed() is False


# ── сохранение через мод ─────────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


def _post_returning(monkeypatch, response=None, error=None):
    sent = []

    def post(url, json=None, headers=None, timeout=None):
        sent.append((url, json, timeout))
        if error:
            raise error
        return response
    monkeypatch.setattr(controller.httpx, "post", post)
    monkeypatch.setattr(controller, "_game_port_open", lambda: True)
    return sent


_REAL_SAVE = controller._save_game


def test_save_game_sends_the_mod_command(monkeypatch):
    sent = _post_returning(monkeypatch, FakeResponse(200, {"status": "ok", "result": "{\"saved\":true}"}))
    assert _REAL_SAVE("autosave_agent", 20) == (True, "autosave_agent")
    assert sent[0][1] == {"type": "save_game", "name": "autosave_agent"} and sent[0][2] == 20


def test_save_game_old_mod_has_no_command(monkeypatch):
    _post_returning(monkeypatch, FakeResponse(200, {"status": "error", "message": "ERROR: unknown command: save_game (мод старее)"}))
    ok, why = _REAL_SAVE("autosave_agent", 20)
    assert not ok and "без команды сохранения" in why


def test_save_game_timeout_and_busy_mod(monkeypatch):
    _post_returning(monkeypatch, error=httpx.ReadTimeout("t"))
    assert _REAL_SAVE("a", 20) == (False, "не записано за 20 с")
    _post_returning(monkeypatch, FakeResponse(202, {"status": "queued_timeout"}))
    assert not _REAL_SAVE("a", 20)[0]
    monkeypatch.setattr(controller, "_game_port_open", lambda: False)
    assert _REAL_SAVE("a", 20) == (False, "игра не отвечает")


# ── манифест и обработчик инструмента ─────────────────────────────────────────────────────────────

async def test_manifest_has_restart_game_next_to_launch_game():
    manifest = await module_manifest.build()
    tools = {t["name"]: t for t in manifest["tools"]}
    spec = tools["restart_game"]
    assert 0 < len(spec["description"]) <= 300
    assert spec["risk"] == tools["launch_game"]["risk"] and spec["availability"] == tools["launch_game"]["availability"]
    assert spec["params"]["reason"]["required"] is True and spec["params"]["save_first"]["default"] is True
    assert {"перезапусти", "рестарт", "перезапуск игры", "restart", "зависла игра"} <= set(spec["keywords"])
    assert not spec.get("node")                                              # вне дерева узлов, как launch_game


class FakeClient:
    def __init__(self):
        self.tools, self.requests = {}, {}

    def tool(self, name):
        return lambda fn: self.tools.setdefault(name, fn)

    def on_request(self, name):
        return lambda fn: self.requests.setdefault(name, fn)


async def test_attach_handler_validates_and_passes_arguments():
    client, calls = FakeClient(), []

    async def restart(reason, save_first):
        calls.append((reason, save_first))
        if reason == "лимит":
            raise RuntimeError("перезапуск был 5 с назад")
        return "игра перезапущена"
    module_manifest.attach(client, [], graph=lambda: {}, restart=restart)
    handler = client.tools["restart_game"]
    assert await handler({"reason": "зависла"}) == {"result": "игра перезапущена"}
    assert await handler({"reason": "зависла", "save_first": False}) == {"result": "игра перезапущена"}
    assert calls == [("зависла", True), ("зависла", False)]
    with pytest.raises(ToolError, match="reason"):
        await handler({"reason": " "})
    with pytest.raises(ToolError, match="перезапуск был 5 с назад"):
        await handler({"reason": "лимит"})
    module_manifest.attach(c2 := FakeClient(), [], graph=lambda: {})
    with pytest.raises(ToolError, match="не подключён"):
        await c2.tools["restart_game"]({"reason": "x"})
