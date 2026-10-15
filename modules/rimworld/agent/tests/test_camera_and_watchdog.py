"""Режиссёр камеры и наблюдатель за игрой: решения без игры."""
import random

import camera_director as cd
import game_watchdog as gw


def pawn(i, x, z, job="Mine", **kw):
    return {"id": i, "position": {"x": x, "z": z}, "current_job": job, **kw}


def state(pawns, threats=()):
    return {"colony": {"pawns": pawns}, "threats": list(threats)}


def test_first_update_enables_auto_mode_once():
    d = cd.CameraDirector()
    first = d.update(state([pawn("a", 10, 10)]), now=100)
    assert first[0] == {"type": "camera_mode", "mode": "auto"}
    assert all(c["type"] != "camera_mode" for c in d.update(state([pawn("a", 10, 10)]), now=101))


def test_idle_follows_busy_pawn_softly_and_alternates_wide_shot():
    d = cd.CameraDirector(random.Random(1))
    s = state([pawn("a", 10, 10, "Mine"), pawn("b", 30, 30, "Wait_Wander")])
    shots = []
    for i in range(1, 9):
        shots += [c for c in d.update(s, now=i * 100) if c["type"] != "camera_mode"]
    assert any(c["type"] == "camera_track" and c["pawn_id"] == "a" and c["soft"] for c in shots)
    assert any(c["type"] == "camera_goto" and c["size"] >= 24 and c["soft"] for c in shots)     # общий план


def test_death_shot_uses_last_known_position():
    d = cd.CameraDirector()
    d.update(state([pawn("a", 40, 50)]), now=1)
    cmds = d.on_event({"type": "pawn_died", "data": {"pawn_id": "a"}}, state([]), now=2)
    assert cmds[0]["x"] == 40 and cmds[0]["z"] == 50 and not cmds[0]["soft"] and cmds[0]["size"] <= 12


def test_fight_frames_threats_and_zooms_by_spread():
    d = cd.CameraDirector()
    near = state([pawn("a", 50, 50, is_drafted=True)], [{"position": {"x": 54, "z": 50}}])
    far = state([pawn("a", 50, 50, is_drafted=True)], [{"position": {"x": 110, "z": 50}}])
    c1 = [c for c in d.update(near, now=10) if c["type"] == "camera_goto"][0]
    c2 = [c for c in cd.CameraDirector().update(far, now=10) if c["type"] == "camera_goto"][0]
    assert c1["x"] == 52 and c2["size"] > c1["size"]


def base(**kw):
    s = {"game_running": True, "launch_ok": True, "mod_ready": True, "in_game": True, "menu_for_s": 0, "window": "front",
         "need_front": True, "recording_wanted": True, "recording": "alive", "camera_wanted": True,
         "camera_mode": "auto"}
    return {**s, **kw}


def test_watchdog_launches_game_only_when_allowed():
    # ТЗ 11e, POL-12: сторож поднимает игру только при фокусе модуля (launch_allowed), не по умолчанию
    assert gw.plan_actions(base(game_running=False)) == []
    assert gw.plan_actions(base(game_running=False, launch_allowed=True)) == ["launch_game"]
    assert gw.plan_actions(base(game_running=False, launch_allowed=True, launch_ok=False)) == []


def test_watchdog_waits_while_mod_loads():
    assert gw.plan_actions(base(mod_ready=False, in_game=False, window="minimized")) == []


def test_watchdog_restores_minimized_window_and_covered_window_only_when_recording():
    assert gw.plan_actions(base(window="minimized", need_front=False, recording_wanted=False,
                                camera_wanted=False)) == ["show_window"]
    assert "show_window" not in gw.plan_actions(base(window="background", need_front=False))
    assert "show_window" in gw.plan_actions(base(window="background"))


def test_watchdog_creates_colony_after_grace_in_menu():
    assert gw.plan_actions(base(in_game=False, menu_for_s=10)) == []
    assert gw.plan_actions(base(in_game=False, menu_for_s=gw.MENU_GRACE_S)) == ["new_colony"]


def test_watchdog_restarts_dead_recording_and_camera_mode():
    acts = gw.plan_actions(base(recording="dead", camera_mode="off"))
    assert acts == ["restart_recording", "camera_mode"]
    assert gw.plan_actions(base(recording="dead", window="minimized")) == ["show_window"]
