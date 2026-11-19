"""Замер колонии: провал прогона, если игра стоит на паузе и ничего не происходит (критерий stall)."""
import colony_metrics as cm
from test_colony_eval import FakeMod, ce, go
from test_colony_metrics import crit, st


def paused_run(times, mutate=None, cfg=None):
    """Пауза в моменты times (с); mutate(i, состояние) вносит изменение на i-м опросе."""
    m = cm.ColonyMetrics(cfg or cm.Config())
    for i, t in enumerate(times):
        s = st(1000, paused=True)
        if mutate:
            mutate(i, s)
        m.update(s, t)
    return m


def test_boundary_59_and_60():
    assert not paused_run([0, 59]).stall["failed"]
    m = paused_run([0, 30, 60])
    assert m.stall["failed"] and m.stall["paused_s"] == 60 and m.stall["at_real_s"] == 60
    assert crit(m, "stall", final=False)["status"] == "FAIL" and m.exit_code() == 1
    assert m.report()["stall"]["failed"] and m.report()["exit_code"] == 1


def test_param_boundaries():
    assert paused_run([0, 10], cfg=cm.Config(stall_seconds=10)).stall["failed"]
    assert not paused_run([0, 9], cfg=cm.Config(stall_seconds=10)).stall["failed"]
    assert not paused_run([0, 500], cfg=cm.Config(stall_seconds=0)).stall["failed"]       # проверка выключена


def test_pause_with_changes_is_not_a_failure():
    def mut(i, s):                                          # агент на паузе ставит чертежи каждые 30 с
        s["colony"]["pending_construction"] = [{"def": "Wall", "status": "blueprint"}] * i
    m = paused_run([0, 30, 60, 90, 120], mut)
    assert not m.stall["failed"] and m.longest_still == 0
    m2 = paused_run([0, 30, 60, 90], lambda i, s: s["colony"].update(resources={"WoodLog": 50 - i}))
    assert not m2.stall["failed"]


def test_change_resets_countdown():
    def mut(i, s):
        if i >= 2:
            s["colony"]["zones"] = [{"id": "z1"}]
    m = paused_run([0, 30, 50, 80, 109], mut)               # изменение на 50 с: отсчёт заново, 59 с до конца
    assert not m.stall["failed"]
    m2 = paused_run([0, 30, 50, 80, 110], mut)
    assert m2.stall["failed"] and m2.stall["last_change"]["what"] == ["zones"] and m2.stall["last_change"]["at_real_s"] == 50


def test_several_pauses_unpause_resets():
    m = cm.ColonyMetrics()
    for t, paused in [(0, True), (40, True), (45, False), (50, True), (100, True)]:
        m.update(st(1000 + t, paused=paused), t)
    assert not m.stall["failed"] and m.longest_still == 50    # две паузы по 40 и 50 с, снятие паузы сбросило отсчёт
    m.update(st(1100, paused=True), 110)
    assert m.stall["failed"] and m.stall["paused_s"] == 60


def test_events_count_as_change_service_events_do_not():
    def service(i, s):
        s["pending_events"] = [{"type": "game_loaded", "tick": i, "data": {}}]

    def letter(i, s):
        s["pending_events"] = [{"type": "letter", "tick": i, "data": {}}]
    assert paused_run([0, 70], service).stall["failed"]
    assert not paused_run([0, 70], letter).stall["failed"]


def test_run_stops_immediately_with_stop_on_fail():
    mod = FakeMod([st(0)] + [st(300, paused=True)] * 200)
    code, rep, log = go(mod, cm.Config(active_seconds=1800), stop_on_fail=True)
    assert code == 1 and rep["stall"]["failed"] and rep["exit_code"] == 1 and any("ПРОВАЛ" in x for x in log)
    assert rep["time"]["wall_seconds"] < 100


def test_run_continues_by_default_and_ends_with_1():
    mod = FakeMod([st(0)] + [st(300, paused=True)] * 14 + [st(300 + i * 300) for i in range(1, 30)])
    code, rep, log = go(mod, cm.Config(active_seconds=40))
    assert code == 1 and rep["stall"]["failed"] and rep["time"]["active_seconds"] >= 40
    assert sum("ПРОВАЛ" in x for x in log) == 1


def test_cli_args():
    a = ce.build_parser().parse_args(["--stall-seconds", "30", "--stop-on-fail"])
    assert ce.config_from_args(a).stall_seconds == 30 and a.stop_on_fail
