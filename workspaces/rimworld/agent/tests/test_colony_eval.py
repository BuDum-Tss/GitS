"""Инструмент замера colony_eval: цикл опроса с подделанным модом, часами и кодами возврата (игра не нужна)."""
import importlib.util
import json
import sys
from pathlib import Path

import colony_metrics as cm
from test_colony_metrics import bld, enemy, furnished, probe_info, st

_path = Path(__file__).resolve().parents[2] / "tools" / "colony_eval.py"
_spec = importlib.util.spec_from_file_location("colony_eval", _path)
ce = importlib.util.module_from_spec(_spec)
sys.modules["colony_eval"] = ce
_spec.loader.exec_module(ce)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeMod:
    """Мод: список состояний по опросам (None: сбой связи), ответы команд."""

    def __init__(self, states, room_support=True, raid_support=True):
        self.states, self.i, self.cmds = states, 0, []
        self.room_support, self.raid_support = room_support, raid_support

    def get_state(self):
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        if s is None:
            raise ce.ModError("нет связи")
        return s

    def command(self, cmd):
        self.cmds.append(cmd)
        if cmd["type"] == "room_info":
            if not self.room_support:
                return False, "ERROR: unknown command: room_info"
            return True, json.dumps(probe_info())
        if cmd["type"] == "dev_trigger_incident":
            return (True, "OK") if self.raid_support else (False, "unknown command: dev_trigger_incident")
        return False, "?"


def go(mod, cfg=None, **kw):
    c = Clock()
    log = []
    cfg = cfg or cm.Config(active_seconds=40)
    code, rep = ce.run(cfg, mod, now=c.now, sleep=c.sleep, out=log.append, **kw)
    return code, rep, log


def scenario(n=12):
    out = [st(0)]
    for i in range(1, n):
        out.append(st(i * 300, threats=[enemy()] if i in (3, 4) else [], buildings=furnished() if i >= 2 else []))
    return out


def test_full_run_passes_with_exit_code_0():
    code, rep, _ = go(FakeMod(scenario()))
    assert code == 0 and rep["passed"] and rep["time"]["active_seconds"] >= 40


def test_failing_run_exit_code_1():
    code, rep, _ = go(FakeMod([st(i * 300) for i in range(12)]))
    assert code == 1 and not rep["passed"]


def test_game_closed_aborts_with_code_2():
    states = [st(0), st(300)] + [None] * 100
    code, rep, _ = go(FakeMod(states), abort_after=60)
    assert code == 2 and rep["aborted"] and "не отвечает" in rep["abort_reason"]


def test_short_outage_survived():
    states = [st(0), st(300), None, None, st(900)] + [st(900 + i * 300) for i in range(1, 20)]
    code, rep, _ = go(FakeMod(states), abort_after=60)
    assert code != 2 and rep["time"]["misses"] == 2


def test_menu_forever_aborts():
    code, rep, _ = go(FakeMod([{"status": "no_game"}]), abort_after=30)
    assert code == 2 and "не загружена" in rep["abort_reason"]


def test_once_mode():
    code, rep, log = go(FakeMod([st(100)]), once=True)
    assert code == 0 and "колонистов 2" in log[-1]
    code2, _, _ = go(FakeMod([{"status": "no_game"}]), once=True)
    assert code2 == 2


def test_raid_injection_marks_attack():
    mod = FakeMod([st(i * 300) for i in range(40)])
    orig = mod.command

    def command(cmd):
        if cmd["type"] == "dev_trigger_incident":           # враги появляются сразу после инъекции
            for k in range(mod.i, mod.i + 2):
                mod.states[k]["threats"] = [enemy()]
        return orig(cmd)
    mod.command = command
    code, rep, log = go(mod, cm.Config(active_seconds=80), raid_at=10, raid_points=300)
    inj = [c for c in mod.cmds if c["type"] == "dev_trigger_incident"]
    assert len(inj) == 1 and inj[0] == {"type": "dev_trigger_incident", "incident": "RaidEnemy", "points": 300}
    assert rep["attacks"]["list"][0]["injected"] is True
    assert any("инъектирован" in x for x in log)


def test_raid_unsupported_warns_and_continues():
    code, rep, log = go(FakeMod(scenario(), raid_support=False), raid_at=5)
    assert any("не знает dev_trigger_incident" in x for x in log)
    assert rep["attacks"]["injections"][0]["supported"] is False and rep["time"]["active_seconds"] >= 40


def test_room_info_unsupported_falls_back_to_bbox():
    walls = [bld("Wall", x, z) for x in (0, 6) for z in range(0, 7)] + [bld("Wall", x, z) for z in (0, 6) for x in range(1, 6)]
    b = walls + [bld("Door", 3, 0), bld("Bed", 2, 2), bld("DiningChair", 4, 4)]
    states = [st(0)] + [st(i * 300, buildings=b) for i in range(1, 12)]
    code, rep, _ = go(FakeMod(states, room_support=False))
    assert rep["building"]["mode"] == "bbox" and rep["building_done_at_s"] is not None


def test_make_client_modes():
    a = ce.build_parser().parse_args([])
    assert ce.make_client(a, {}).base == "http://localhost:8765"
    c = ce.make_client(a, {"GITS_RIMWORLD_HOST_URL": "http://host:8796/", "GITS_RIMWORLD_HOST_TOKEN": "sekret"})
    assert c.base == "http://host:8796/game-api" and c.token == "sekret"
    a = ce.build_parser().parse_args(["--host-url", "http://h:1", "--host-token-env", "MY_TOK"])
    c = ce.make_client(a, {"MY_TOK": "t2"})
    assert c.base == "http://h:1/game-api" and c._headers()["Authorization"] == "Bearer t2"
    a = ce.build_parser().parse_args(["--api", "http://localhost:9999"])
    assert ce.make_client(a, {"GITS_RIMWORLD_HOST_TOKEN": "x"}).token == ""        # на петле токен не нужен


def test_save_report_writes_json(tmp_path):
    _, rep, _ = go(FakeMod(scenario()))
    p = ce.save_report(rep, tmp_path / "eval", "20260101_000000")
    assert p.name == "colony_eval_20260101_000000.json" and json.loads(p.read_text(encoding="utf-8"))["exit_code"] == 0


def test_config_from_args():
    a = ce.build_parser().parse_args(["--active-seconds", "60", "--min-beds", "3", "--max-losses", "0", "--strict-work"])
    cfg = ce.config_from_args(a)
    assert cfg.active_seconds == 60 and cfg.min_beds == 3 and cfg.max_losses == 0 and cfg.strict_work
