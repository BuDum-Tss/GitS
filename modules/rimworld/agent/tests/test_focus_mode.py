"""Режим фокуса модуля RimWorld (правка fix/focus-bootstrap, ТЗ 11e POL-11/POL-12/POL-14): в режиме auto модуль без менеджера
фокуса в ядре сам берёт фокус, когда игра запущена, и уходит в afk, когда она закрыта; игру не запускает. Режимы core и manual
сами фокус не берут. Первый запрос ядра отдаёт фокус ядру. Кнопки панели «Взять фокус» и «Отпустить фокус».

Игра, Claude и локальная модель не нужны: состояние, пинг и команды мода подменены."""
import json

import pytest
from gits_sdk import protocol as P

import controller
import focus_checkpoint
import focus_mode
import game_launcher
import game_panel
import game_modes
import module_manifest
import planning_pause
import policy_manager
import watch_rules


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
    sent: list[dict] = []
    monkeypatch.setattr(controller, "_game_command", lambda cmd: sent.append(cmd) or True)
    monkeypatch.setattr(controller, "_ping", lambda: False)
    monkeypatch.setattr(controller, "_get_events", lambda: [])
    monkeypatch.setattr(controller, "_get_state", lambda: {})          # игра закрыта, пока тест не скажет иначе
    c = controller.Controller({"memory_dir": str(tmp_path)})
    c._ensure_run_started = lambda state: None
    c.sent = sent
    c.tmp = tmp_path
    return c


def use_mode(ctrl, monkeypatch, mode):
    """Настройка focus_mode (в живом модуле её присылает ядро через link.module_settings)."""
    monkeypatch.setattr(ctrl, "_module_setting", lambda key, default: mode if key == "focus_mode" else default)


def game_up(ctrl, monkeypatch, tick=1000):
    monkeypatch.setattr(controller, "_get_state", lambda: {"tick": tick, "paused": False, "save_id": "s1", "threats": [],
                                                           "colony": {"pawns": [], "resources": {}, "buildings": [],
                                                                      "pending_construction": []}})


def game_closed(monkeypatch):
    monkeypatch.setattr(controller, "_get_state", lambda: {})


def no_launch(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("игра не должна запускаться модулем")
    monkeypatch.setattr(game_launcher, "launch", boom)
    monkeypatch.setattr(controller.Controller, "_launch_rimworld", lambda self, reason="": boom())


# ── чистые функции ───────────────────────────────────────────────────────────────────────────

def test_normalize_keeps_known_modes_and_defaults_the_rest():
    assert [focus_mode.normalize(m) for m in ("auto", "core", "manual")] == ["auto", "core", "manual"]
    assert focus_mode.normalize("bogus") == "core" and focus_mode.normalize(None) == "core"


def test_auto_grab_rule():
    base = dict(mode="auto", core_seen=False, game_up=True, in_focus=False, held_off=False)
    assert focus_mode.auto_grab_allowed(**base) is True
    assert focus_mode.auto_grab_allowed(**{**base, "mode": "core"}) is False
    assert focus_mode.auto_grab_allowed(**{**base, "mode": "manual"}) is False
    assert focus_mode.auto_grab_allowed(**{**base, "core_seen": True}) is False
    assert focus_mode.auto_grab_allowed(**{**base, "game_up": False}) is False
    assert focus_mode.auto_grab_allowed(**{**base, "in_focus": True}) is False
    assert focus_mode.auto_grab_allowed(**{**base, "held_off": True}) is False


def test_afk_explanation_names_the_real_reason():
    assert "не запущена" in focus_mode.afk_explanation(mode="auto", core_seen=False, game_up=False, held_off=False)
    assert "вручную" in focus_mode.afk_explanation(mode="manual", core_seen=False, game_up=True, held_off=False)
    assert "policy.activate" in focus_mode.afk_explanation(mode="auto", core_seen=True, game_up=True, held_off=False)
    assert "отпущен" in focus_mode.afk_explanation(mode="auto", core_seen=False, game_up=True, held_off=True)


# ── режим auto ───────────────────────────────────────────────────────────────────────────────

async def test_auto_takes_focus_when_the_game_runs_without_a_core_manager(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch, tick=1000)
    assert ctrl.policies.current == "afk"
    await ctrl._tick()
    assert ctrl.policies.current == "observe" and ctrl.policies.in_focus      # фокус взят сам
    assert ctrl._focus_source == "auto" and ctrl._tick_now == 1000           # тик игры идёт в фокусе
    assert ctrl.policies.history[-1]["source"] == "auto"                     # журнал/state видят источник
    assert ctrl.policy_state()["focus"]["source"] == "auto"


async def test_auto_stays_afk_and_never_launches_while_the_game_is_closed(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "auto")
    game_closed(monkeypatch)
    for _ in range(5):
        await ctrl._tick()
    assert ctrl.policies.current == "afk" and ctrl.sent == []
    focus = ctrl.policy_state()["focus"]
    assert focus["game_up"] is False and "не запущена" in focus["why_afk"]


async def test_auto_leaves_focus_when_the_game_closes(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()
    assert ctrl.policies.in_focus
    game_closed(monkeypatch)
    await ctrl._tick()                       # один пустой опрос: сбой, фокус ещё у модуля
    assert ctrl.policies.in_focus
    await ctrl._tick()
    await ctrl._tick()                       # несколько опросов подряд: игра закрыта
    assert ctrl.policies.current == "afk" and ctrl._focus_source == "none"
    assert ctrl.policies.reason.startswith("игра закрыта")
    assert ctrl.policies.history[-1]["source"] == "auto"


async def test_first_core_request_hands_focus_over_to_the_core(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()                       # модуль сам взял фокус
    res = await ctrl.release({"reason": "фокус у монтажа"})       # первый запрос ядра: менеджер фокуса есть
    assert res["ok"] and ctrl.policies.current == "afk"
    assert ctrl.policy_state()["focus"]["core_manager"] is True
    await ctrl._tick()
    await ctrl._tick()
    assert ctrl.policies.current == "afk"    # игра идёт, но модуль подчиняется только ядру: сам не берёт фокус
    act = await ctrl.activate({"policy": "observe", "goal": "вернулась"})
    assert act["ok"] and ctrl.policies.current == "observe" and ctrl._focus_source == "core"


async def test_first_core_activate_in_afk_also_disables_auto(ctrl, monkeypatch):
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    act = await ctrl.activate({"policy": "observe"})
    assert act["ok"] and ctrl.policies.current == "observe"
    await ctrl._tick()
    assert ctrl._focus_source == "core" and ctrl.policies.current == "observe"


# ── режимы core и manual ─────────────────────────────────────────────────────────────────────

async def test_core_mode_never_grabs_focus_by_itself(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "core")
    game_up(ctrl, monkeypatch)
    for _ in range(3):
        await ctrl._tick()
    assert ctrl.policies.current == "afk"
    assert (await ctrl.activate({"policy": "observe"}))["ok"] is True              # ядро по-прежнему отдаёт фокус


async def test_manual_mode_never_grabs_and_refuses_core_requests(ctrl, monkeypatch):
    no_launch(monkeypatch)
    use_mode(ctrl, monkeypatch, "manual")
    game_up(ctrl, monkeypatch)
    for _ in range(3):
        await ctrl._tick()
    assert ctrl.policies.current == "afk"
    res = await ctrl.activate({"policy": "observe"})
    assert res["ok"] is False and "вручную" in res["error"] and ctrl.policies.current == "afk"
    assert "вручную" in ctrl.policy_state()["focus"]["why_afk"]


# ── кнопки панели ────────────────────────────────────────────────────────────────────────────

async def test_take_button_restores_focus_and_refuses_when_the_game_is_closed(ctrl, monkeypatch):
    use_mode(ctrl, monkeypatch, "manual")
    game_closed(monkeypatch)
    monkeypatch.setattr(controller, "_ping", lambda: False)
    res = await ctrl.panel_take_focus({})
    assert res["ok"] is False and "не запущена" in res["message"] and ctrl.policies.current == "afk"
    monkeypatch.setattr(controller, "_ping", lambda: True)
    game_up(ctrl, monkeypatch)
    res = await ctrl.panel_take_focus({})
    assert res["ok"] is True and ctrl.policies.current == "observe" and ctrl._focus_source == "button"
    assert (await ctrl.panel_take_focus({}))["message"] == "фокус уже у модуля"


async def test_release_button_checkpoints_pauses_and_holds_auto_off(ctrl, monkeypatch):
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()                        # авто-фокус
    assert ctrl.policies.in_focus
    res = await ctrl.panel_release_focus({})
    assert res["ok"] and ctrl.policies.current == "afk"
    assert ctrl.sent == [{"type": "pause"}]                                    # игра на паузе, как у focus.release
    assert focus_checkpoint.load()["paused_by_module"] is True
    await ctrl._tick()
    await ctrl._tick()
    assert ctrl.policies.current == "afk"     # отпущено владельцем: сам не берём, игра ещё открыта
    assert "отпущен" in ctrl.policy_state()["focus"]["why_afk"]
    assert (await ctrl.panel_release_focus({}))["message"] == "фокуса и так нет"


async def test_take_button_after_release_restores_checkpoint_and_resumes(ctrl, monkeypatch):
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()
    await ctrl.panel_release_focus({})
    monkeypatch.setattr(controller, "_ping", lambda: True)
    res = await ctrl.panel_take_focus({})
    assert res["ok"] and ctrl.policies.current == "observe"
    assert ctrl.sent[-1] == {"type": "resume"} and focus_checkpoint.load() is None


async def test_closing_the_game_clears_the_hold_so_auto_works_after_restart(ctrl, monkeypatch):
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()
    await ctrl.panel_release_focus({})
    game_closed(monkeypatch)
    for _ in range(focus_mode.GAME_GONE_TICKS):
        await ctrl._tick()
    game_up(ctrl, monkeypatch)
    await ctrl._tick()
    assert ctrl.policies.current == "observe" and ctrl._focus_source == "auto"


# ── состояние и манифест ─────────────────────────────────────────────────────────────────────

async def test_every_transition_is_written_to_the_journal(ctrl, monkeypatch):
    logged: list[str] = []
    ctrl.policies._log = lambda kind, text: logged.append(text)
    use_mode(ctrl, monkeypatch, "auto")
    game_up(ctrl, monkeypatch)
    await ctrl._tick()
    assert any("взял фокус сам" in line and "(auto)" in line for line in logged)


def test_manifest_declares_the_focus_setting_and_panel_actions_and_validates():
    m = module_manifest.build_manifest([])
    assert P.validate_manifest(m, []) == []
    schema = m["settings_schema"]["focus_mode"]
    assert schema["default"] == "core" and schema["enum"] == ["auto", "core", "manual"]
    assert m["panel"]["actions"] == ["take_focus", "release_focus"]


def test_focus_buttons_pass_the_panel_validation():
    blocks = game_panel.policy_blocks({"policy": {"id": "afk", "title": "Вне игры", "reason": "x"},
                                       "focus": {"mode": "auto", "mode_label": "авто", "why_afk": "игра не запущена"}})
    assert [b["type"] for b in blocks] == ["stats", "buttons", "text"]
    assert P.validate_panel_set({"blocks": blocks}, {"take_focus", "release_focus"}) == []
    stats = blocks[0]["items"]
    assert any(i["label"] == "Почему вне фокуса" and "не запущена" in i["value"] for i in stats)
