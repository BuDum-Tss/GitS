"""ТЗ 11c (GT-1..GT-3): игровое время идёт. Решения проверяются чистыми функциями и наблюдателем на подменённых часах и
состоянии мода, без игры."""
import pytest

import game_launcher
import game_watchdog as gw
import planning_pause


# ── GT-3: чистые функции ──

def t(**kw):
    base = {"in_game": True, "paused": True, "fresh_colony": False, "time_wanted": True, "stalled_s": 0.0,
            "stall_limit_s": 180.0, "planner_paused": False, "user_idle_s": 0.0, "since_note_s": None, "announced": False}
    return {**base, **kw}


def test_new_colony_on_pause_is_resumed_at_once():
    assert gw.plan_time_actions(t(fresh_colony=True)) == ["resume_new_colony"]
    assert gw.plan_time_actions(t(fresh_colony=True, paused=False)) == []              # игра уже идёт
    assert gw.plan_time_actions(t(fresh_colony=True, time_wanted=False)) == ["resume_new_colony"]


def test_stalled_pause_without_user_is_resumed():
    assert gw.plan_time_actions(t(stalled_s=179, user_idle_s=999)) == []
    assert gw.plan_time_actions(t(stalled_s=180, user_idle_s=gw.USER_IDLE_MIN_S)) == ["resume_stalled"]


def test_stalled_pause_with_user_is_left_alone_with_one_announcement():
    first = gw.plan_time_actions(t(stalled_s=400, user_idle_s=5))
    assert first == ["note_user_paused", "announce_user_paused"]
    assert gw.plan_time_actions(t(stalled_s=500, user_idle_s=5, since_note_s=100, announced=True)) == []
    assert gw.plan_time_actions(t(stalled_s=1000, user_idle_s=5, since_note_s=600, announced=True)) == ["note_user_paused"]


def test_growing_tick_or_unwanted_time_or_planning_pause_do_nothing():
    assert gw.plan_time_actions(t(paused=False, stalled_s=999, user_idle_s=999)) == []
    assert gw.plan_time_actions(t(stalled_s=999, user_idle_s=999, time_wanted=False)) == []
    assert gw.plan_time_actions(t(stalled_s=999, user_idle_s=999, planner_paused=True)) == []
    assert gw.plan_time_actions(t(in_game=False, stalled_s=999, user_idle_s=999)) == []


def test_track_tick_measures_how_long_the_tick_stands():
    seen, stalled = gw.track_tick(None, 100, 10.0)
    assert seen == (100, 10.0) and stalled == 0.0
    seen, stalled = gw.track_tick(seen, 100, 70.0)
    assert seen == (100, 10.0) and stalled == 60.0
    seen, stalled = gw.track_tick(seen, 101, 80.0)
    assert seen == (101, 80.0) and stalled == 0.0
    assert gw.track_tick(seen, None, 90.0) == (None, 0.0)


def test_stall_limit_is_clamped_to_setting_range():
    assert gw.stall_limit_s(None) == 180 and gw.stall_limit_s("x") == 180
    assert gw.stall_limit_s(5) == 60 and gw.stall_limit_s(99999) == 1800 and gw.stall_limit_s(240) == 240


async def test_stall_unpause_is_a_module_setting():
    import module_manifest
    spec = (await module_manifest.build())["settings_schema"]["stall_unpause_s"]
    assert spec["default"] == 180 and spec["min"] == 60 and spec["max"] == 1800


# ── наблюдатель на подменённой игре ──

class Fake:
    """Подмена игры, часов и пользователя."""

    def __init__(self, monkeypatch, record_runs=True):
        self.now = 1000.0
        self.game = {"status": "ok", "tick": 500, "paused": True, "time_speed": 0}
        self.in_menu = False
        self.idle = 9999.0
        self.commands, self.said, self.logged = [], [], []
        self.settings = {"record_runs": record_runs}
        self.colonies = 0
        monkeypatch.setattr(game_launcher, "is_running", lambda: True)
        monkeypatch.setattr(gw, "window_state", lambda: "front")
        monkeypatch.setattr(planning_pause, "active", lambda *a, **k: False)
        monkeypatch.setattr(gw.events_log, "log", lambda kind, text="", **d: self.logged.append((kind, text)))
        monkeypatch.delenv("GITS_LAUNCH_GAME", raising=False)
        monkeypatch.delenv("GITS_LAUNCH_GAME", raising=False)
        self.wd = gw.GameWatchdog(
            settings_get=lambda: self.settings, recorder=None, launch=lambda: None, new_colony=self.new_colony,
            mod_ready=lambda: True, state_ready=lambda: not self.in_menu, send_camera=lambda cmd: None,
            on_camera_lost=lambda: None, get_state=lambda: dict(self.game), send_command=self.command,
            module_setting=lambda key, default: default, idle_seconds=lambda: self.idle,
            announce=self.said.append, clock=lambda: self.now)

    async def new_colony(self):
        self.colonies += 1
        self.in_menu = False
        self.game.update(tick=0, paused=True, time_speed=0)

    def command(self, cmd):
        self.commands.append(cmd)
        if cmd["type"] == "set_time_speed":
            self.game.update(time_speed=cmd["speed"], paused=cmd["speed"] == 0)
        return True

    async def run(self, seconds, step=15, tick_runs=False):
        for _ in range(int(seconds // step)):
            self.now += step
            if tick_runs and not self.game["paused"]:
                self.game["tick"] += 100
            await self.wd.step()


async def test_new_colony_made_by_watchdog_is_unpaused_and_announced(monkeypatch):
    f = Fake(monkeypatch)
    f.in_menu = True
    await f.run(60)                                        # меню дольше MENU_GRACE_S: создаёт колонию
    assert f.colonies == 1
    await f.run(30)
    assert f.commands == [{"type": "set_time_speed", "speed": 1}] and f.game["paused"] is False
    assert len(f.said) == 1 and "колония создана для записи забега" in f.said[0] and "пауз" in f.said[0]
    assert "Ты сама снимаешь паузу" in f.said[0] and "не пользователь" in f.said[0]      # DP-2: не путать с действием человека
    assert ("video", "пауза снята: скорость 1") in f.logged
    await f.run(300, tick_runs=True)
    assert len(f.commands) == 1                            # дальше тик растёт: ничего не делаем


async def test_time_is_not_our_business_without_recording_or_autolaunch(monkeypatch):
    f = Fake(monkeypatch, record_runs=False)               # запись выключена, автозапуска нет
    await f.run(600)
    assert f.commands == [] and f.said == []


async def test_stalled_tick_without_user_is_unpaused_and_logged(monkeypatch):
    f = Fake(monkeypatch)
    await f.run(150)
    assert f.commands == []                                # 150 с < 180 с
    await f.run(60)
    assert f.commands == [{"type": "set_time_speed", "speed": 1}]
    assert len(f.said) == 1 and "стояла на паузе" in f.said[0] and "Ты сама снимаешь паузу" in f.said[0]
    assert any(kind == "video" and "стояла на паузе" in text for kind, text in f.logged)
    await f.run(300, tick_runs=True)
    assert len(f.commands) == 1


async def test_stalled_tick_with_user_at_computer_is_not_touched_but_announced_once(monkeypatch):
    f = Fake(monkeypatch)
    f.idle = 3.0
    await f.run(1500)
    assert f.commands == [] and f.game["paused"] is True
    assert len(f.said) == 1 and "запись идёт впустую" in f.said[0]
    notes = [text for kind, text in f.logged if kind == "video" and "паузу не трогаю" in text]
    assert 2 <= len(notes) <= 3                            # раз в 10 минут, не каждые 15 с
    f.idle = 9999.0                                        # пользователь ушёл: снимает
    await f.run(30)
    assert f.commands == [{"type": "set_time_speed", "speed": 1}]


async def test_planning_pause_is_not_unpaused(monkeypatch):
    f = Fake(monkeypatch)
    monkeypatch.setattr(planning_pause, "active", lambda *a, **k: True)
    await f.run(600)
    assert f.commands == []


async def test_launch_env_no_longer_makes_time_wanted(monkeypatch):
    # ТЗ 11e, POL-12: переменная GITS_LAUNCH_GAME больше не включает автозапуск и слежение за временем без записи
    f = Fake(monkeypatch, record_runs=False)
    monkeypatch.setenv("GITS_LAUNCH_GAME", "1")
    await f.run(240)
    assert f.commands == []


@pytest.mark.parametrize("name", ["GITS_LAUNCH_GAME", "GITS_LAUNCH_GAME"])
def test_launch_env_on_accepts_both_names(monkeypatch, name):
    monkeypatch.delenv("GITS_LAUNCH_GAME", raising=False)
    monkeypatch.delenv("GITS_LAUNCH_GAME", raising=False)
    assert game_launcher.launch_env_on() is False
    monkeypatch.setenv(name, "1")
    assert game_launcher.launch_env_on() is True


def test_user_idle_seconds_is_a_sane_number():
    assert 0.0 <= gw.user_idle_seconds() < 10 ** 7


# ── GT-4: живой тест с настоящей RimWorld ──

@pytest.mark.game
async def test_real_game_time_runs_after_watchdog_creates_colony(monkeypatch):
    """Настоящая RimWorld (только `-All` или `-m game`): запуск объявляется пользователю, наблюдатель создаёт колонию
    «Быстрым запуском», снимает паузу и объявляет это; за 60 с игровой тик должен вырасти. Игра закрывается в конце.
    Чужую (уже запущенную) игру тест не трогает."""
    import asyncio
    import subprocess
    import time

    import controller as ctl
    if game_launcher.is_running():
        pytest.skip("RimWorld уже запущена: тест не трогает чужую игру")
    if not game_launcher.find_exe():
        pytest.skip("RimWorld не установлена")
    told = []
    real_post = game_launcher._post_notify
    game_launcher.set_notifier(None)
    monkeypatch.setattr(game_launcher, "_post_notify", lambda text: told.append(text) or real_post(text))

    async def new_colony():
        if not await asyncio.to_thread(gw.press_quick_start):
            return
        for _ in range(45):
            await asyncio.sleep(2)
            if ctl._state_ready(await asyncio.to_thread(ctl._get_state)):
                return

    wd = gw.GameWatchdog(
        settings_get=lambda: {"record_runs": False}, recorder=None, launch=lambda: None, new_colony=new_colony,
        mod_ready=ctl._ping, state_ready=lambda: ctl._state_ready(ctl._get_state()), send_camera=ctl._camera_command,
        on_camera_lost=lambda: None, get_state=ctl._get_state, send_command=ctl._game_command)
    saw_pause = False
    try:
        await asyncio.to_thread(game_launcher.launch, "тест ТЗ 11c: время в игре идёт", 150.0, game_launcher.windowed_args())
        deadline = time.time() + 420
        state, ready_at = {}, None
        while time.time() < deadline:
            await wd.step()
            state = await asyncio.to_thread(ctl._get_state)
            if ctl._state_ready(state):
                ready_at = ready_at or time.time()
                saw_pause = saw_pause or bool(state.get("paused"))
                # после входа в колонию даём наблюдателю 25 с: игра может поставить паузу не сразу
                if time.time() - ready_at > 25 and not state.get("paused"):
                    break
            await asyncio.sleep(3)
        assert ctl._state_ready(state) and not state.get("paused"), f"колония не создана или пауза не снята: {state.get('status')}"
        assert state.get("time_speed") == 1
        tick0 = state["tick"]
        await asyncio.sleep(60)
        state = await asyncio.to_thread(ctl._get_state)
        assert state["tick"] > tick0 and not state.get("paused"), f"тик не вырос за 60 с: {tick0} -> {state['tick']}"
    finally:
        await asyncio.to_thread(subprocess.run, ["taskkill", "/IM", game_launcher.PROCESS, "/F"], capture_output=True)   # свою игру закрываем
    assert any(text.startswith("Ты сама запускаешь RimWorld (не пользователь): тест ТЗ 11c") for text in told)
    if saw_pause:                                          # игра ставила паузу сама: наблюдатель обязан был это объявить
        assert any("колония создана для записи забега" in text for text in told)


# ── GT-5: ошибки игры приходят агенту из state.game_log ──

def test_new_game_errors_are_reported_once_and_capped():
    import game_errors as ge
    log = {"errors": 2, "last_errors": ["a", "b"]}
    seen, lines = ge.new_errors(log, 0)
    assert seen == 2 and lines == ["a", "b"]
    assert ge.new_errors(log, seen) == (2, [])                                    # те же ошибки второй раз не пишутся
    seen, lines = ge.new_errors({"errors": 3, "last_errors": ["a", "b", "c"]}, seen)
    assert seen == 3 and lines == ["c"]
    seen, lines = ge.new_errors({"errors": 50, "last_errors": [f"e{i}" for i in range(20)]}, 3)
    assert seen == 50 and lines == ["e17", "e18", "e19"]                          # лавина: только последние строки
    assert ge.new_errors({"errors": 1, "last_errors": ["x"]}, 40) == (1, ["x"])     # игра перезапущена: счёт заново
    assert ge.new_errors(None, 5) == (5, []) and ge.new_errors({"errors": "?"}, 5) == (5, [])
