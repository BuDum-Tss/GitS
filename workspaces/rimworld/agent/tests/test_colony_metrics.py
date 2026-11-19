"""Замер колонии: чистая логика colony_metrics на подделанных последовательностях состояний."""
import json

import colony_metrics as cm


def pawn(pid="p1", job="Mine", hp=1.0, mood=0.6, downed=False, drafted=False, prisoner=False, **extra):
    return {"id": pid, "name": pid.upper(), "health_pct": hp, "mood_pct": mood, "current_job": job, "is_downed": downed,
            "is_drafted": drafted, "is_prisoner": prisoner, "position": {"x": 10, "z": 10}, **extra}


def bld(d, x, z):
    return {"id": f"{d}{x}{z}", "def": d, "position": {"x": x, "z": z}}


def st(tick, pawns=None, paused=False, speed=1, threats=None, buildings=None, events=None, save="s", food=8.0, **extra):
    return {"tick": tick, "paused": paused, "time_speed": speed, "save_id": save,
            "colony": {"pawns": pawns if pawns is not None else [pawn("a"), pawn("b")], "buildings": buildings or [],
                       "resources": {"WoodLog": 50}, "food_days": food},
            "threats": threats or [], "pending_events": events or [], **extra}


def run(states, step=5.0, cfg=None, rooms=None):
    m = cm.ColonyMetrics(cfg or cm.Config())
    t = 0.0
    for s in states:
        m.update(s, t, rooms)
        t += step
    return m


def crit(m, cid, final=True):
    return next(c for c in m._criteria(final) if c["id"] == cid)


def test_active_time_and_pauses():
    states = [st(1000), st(1300), st(1600, paused=True), st(1600, paused=True), st(1900, speed=3), st(2200, speed=3)]
    m = run(states)
    assert m.active_seconds == 20.0          # интервалы 1, 2, 3 (пауза включилась, тики шли), 5, 6; четвёртый на паузе без тиков
    assert m.paused_seconds == 5.0 and m.active_ticks == 1200
    assert m.speed_seconds[3] == 10.0


def test_stalled_when_not_paused_but_no_ticks():
    m = run([st(1000), st(1000), st(1000)])
    assert m.active_seconds == 0 and m.stalled_seconds == 10.0


def test_gap_is_not_counted_as_active():
    m = cm.ColonyMetrics(cm.Config(max_gap=30))
    m.update(st(1000), 0)
    m.update(st(2000), 100)               # 100 с без опроса: засчитано только 30
    assert m.active_seconds == 30 and m.gap_seconds == 70


def test_unloaded_game_does_not_accumulate_and_is_not_crash():
    m = cm.ColonyMetrics()
    m.update({"status": "no_game"}, 0)
    m.update(None, 5)
    m.update({"tick": None}, 10)
    m.update(st(100), 15)
    m.update(st(400), 20)
    assert m.active_seconds == 5.0 and m.instant["loaded"]
    m.update({"status": "no_map"}, 25)
    assert m.instant == {"loaded": False}


def test_reload_resets_interval():
    m = run([st(5000), st(5300), st(100, save="other"), st(400, save="other")])
    assert m.reloads == 1 and m.active_seconds == 10.0


def test_miss_counter():
    m = cm.ColonyMetrics()
    m.note_miss(1)
    m.note_miss(2)
    m.update(st(1), 3)
    assert m.misses == 2 and m.consecutive_misses == 0


def furnished(beds=1, tables=1, walls=4):
    b = [bld("Wall", i, 0) for i in range(walls)] + [bld("Door", 5, 0)]
    b += [bld("Bed", 2 + i, 2) for i in range(beds)] + [bld("Table2x2c", 3, 3)] * tables
    return b


def probe_info(**kw):
    return {"room": True, "is_proper_room": True, "role": "Bedroom", "cells": 20, "outdoors": False, "roofed": True, **kw}


def test_building_room_info_ok():
    b = furnished(beds=1)
    rooms = {(2, 2): probe_info(), (3, 3): probe_info()}
    res = cm.evaluate_building(b, 2, cm.Config(), rooms)
    assert res["ok"] and res["beds"] == 1 and res["furniture"] == 1 and not res["approximate"]


def test_building_not_roofed_or_open_or_unfurnished():
    cfg = cm.Config()
    assert not cm.evaluate_building(furnished(), 2, cfg, {(2, 2): probe_info(roofed=False), (3, 3): probe_info(roofed=False)})["ok"]
    assert not cm.evaluate_building(furnished(), 2, cfg, {(2, 2): probe_info(is_proper_room=False)})["ok"]
    assert not cm.evaluate_building(furnished(tables=0), 2, cfg, {(2, 2): probe_info()})["ok"]          # мебели нет
    assert not cm.evaluate_building(furnished(beds=1), 6, cfg, {(2, 2): probe_info(), (3, 3): probe_info()})["ok"]   # нужно 3 кровати
    assert not cm.evaluate_building(furnished(), 2, cfg, {})["ok"]


def test_building_double_bed_and_min_beds_override():
    b = [bld("DoubleBed", 2, 2), bld("Stool", 3, 3)]
    rooms = {(2, 2): probe_info(), (3, 3): probe_info()}
    assert cm.evaluate_building(b, 4, cm.Config(), rooms)["ok"]            # 2 места, нужно ceil(4/2)=2
    assert not cm.evaluate_building(b, 4, cm.Config(min_beds=3), rooms)["ok"]


def test_building_bbox_fallback():
    b = [bld("Wall", x, z) for x in (0, 6) for z in range(0, 7)] + [bld("Wall", x, z) for z in (0, 6) for x in range(1, 6)]
    b += [bld("Door", 3, 0), bld("Bed", 2, 2), bld("DiningChair", 4, 4)]
    res = cm.evaluate_building(b, 2, cm.Config(), None)
    assert res["ok"] and res["approximate"] and res["mode"] == "bbox"
    assert not cm.evaluate_building([x for x in b if x["def"] != "Door"], 2, cm.Config(), None)["ok"]


def test_building_criterion_needs_new_structures_and_latches():
    m = cm.ColonyMetrics()
    m.update(st(0, buildings=[]), 0, {})                       # старт без стен
    m.update(st(300, buildings=furnished()), 5, {(2, 2): probe_info(), (3, 3): probe_info()})
    assert m.building_done_at is not None and crit(m, "building")["status"] == "PASS"
    m.update(st(600, buildings=[]), 10, {})                    # здание пропало: достижение уже зафиксировано
    assert crit(m, "building")["status"] == "PASS"
    # найденное, а не построенное: стены уже были в начале
    m2 = cm.ColonyMetrics()
    m2.update(st(0, buildings=furnished()), 0, {(2, 2): probe_info(), (3, 3): probe_info()})
    m2.update(st(300, buildings=furnished()), 5, {(2, 2): probe_info(), (3, 3): probe_info()})
    assert m2.building_done_at is None and crit(m2, "building")["status"] == "FAIL"
    assert crit(m2, "building", final=False)["status"] == "PENDING"


def test_probe_cells_order_and_limit():
    cells = cm.probe_cells([bld("Table2x2c", 9, 9), bld("Bed", 1, 1), bld("Wall", 0, 0), bld("Bed", 1, 2)], 2)
    assert cells == [(1, 1), (1, 2)]


def enemy(i="e1", downed=False, **kw):
    return {"id": i, "name": "Рейдер", "kind": "human", "is_downed": downed, "position": {"x": 12, "z": 12}, **kw}


def test_attack_started_and_repelled_with_reaction():
    states = [st(0), st(300, threats=[enemy("e1"), enemy("e2")]), st(600, threats=[enemy("e1")], pawns=[pawn("a", drafted=True), pawn("b")]),
              st(900, threats=[enemy("e1", downed=True)]), st(1200), st(1500)]
    m = run(states)
    a = m.attacks[0]
    assert a["status"] == "repelled" and a["peak"] == 2 and a["reaction_s"] == 5.0 and not a["injected"]
    assert crit(m, "attack")["status"] == "PASS"


def test_attack_not_repelled_and_far_wanderers_ignored():
    m = run([st(0), st(300, threats=[enemy()]), st(600, threats=[enemy()])])
    assert crit(m, "attack")["status"] == "FAIL" and m.report()["attacks"]["started"] == 1
    far = enemy(job="GotoWander", position={"x": 200, "z": 200})
    m2 = run([st(0), st(300, threats=[far]), st(600, threats=[far])])
    assert m2.report()["attacks"]["started"] == 0


def test_attack_lost_when_colony_dies():
    m = run([st(0), st(300, threats=[enemy()]), st(600, pawns=[])])
    assert m.attacks[0]["status"] == "lost" and crit(m, "attack")["status"] == "FAIL"


def test_injected_attack_flag():
    m = cm.ColonyMetrics()
    m.update(st(0), 0)
    m.update(st(300), 5)
    m.note_injection(True, True)
    m.update(st(600, threats=[enemy()]), 10)
    for i, s in enumerate([st(900), st(1200)]):
        m.update(s, 15 + 5 * i)
    assert m.attacks[0]["injected"] is True
    m2 = cm.ColonyMetrics()
    m2.note_injection(False, False, "unknown command")
    assert m2.injections[0]["supported"] is False


def test_incident_event_is_deduplicated_and_attached():
    ev = {"type": "incident", "tick": 5, "data": {"incident": "RaidEnemy", "points": 100}}
    m = run([st(0, events=[ev]), st(300, events=[ev], threats=[enemy()]), st(600), st(900), st(1200)])
    assert len(m.incidents) == 1 and m.attacks[0]["incident"] == "RaidEnemy"


def test_work_share_and_thresholds():
    busy = [pawn("a", "Mine"), pawn("b", "ConstructFinishFrame")]
    half = [pawn("a", "Mine"), pawn("b", "Wait_Wander")]
    lazy = [pawn("a", "Wait"), pawn("b", "GotoWander")]
    m = run([st(0, pawns=busy), st(300, pawns=busy), st(600, pawns=half), st(900, pawns=lazy)])
    assert m.work_samples == [1.0, 0.5, 0.0]
    assert crit(m, "work")["status"] == "FAIL"          # среднее 0,5, но хороших выборок 67% < 70%
    m2 = run([st(0, pawns=busy), st(300, pawns=busy), st(600, pawns=busy), st(900, pawns=half)])
    assert crit(m2, "work")["status"] == "PASS"
    m3 = run([st(0, pawns=busy), st(300, pawns=busy), st(600, pawns=half), st(900, pawns=lazy)], cfg=cm.Config(work_samples_min=0.6))
    assert crit(m3, "work")["status"] == "PASS"


def test_work_ignores_rest_combat_downed_prisoners():
    pawns = [pawn("a", "Mine"), pawn("b", "LayDown"), pawn("c", "AttackMelee"), pawn("d", "Wait", downed=True),
             pawn("e", "Wait", prisoner=True), pawn("f", "Wait_Wander")]
    m = run([st(0, pawns=pawns), st(300, pawns=pawns)])
    assert m.work_samples == [0.5] and m.work_strict_samples == [1 / 3]
    ms = run([st(0, pawns=pawns), st(300, pawns=pawns)], cfg=cm.Config(strict_work=True))
    assert crit(ms, "work")["value"].startswith("среднее 0.33")


def test_idle_streaks_and_empty_hours():
    idle = [pawn("a", "Wait"), pawn("b", "Wait_Wander")]
    work = [pawn("a", "Mine"), pawn("b", "Mine")]
    m = run([st(0, pawns=idle), st(300, pawns=idle), st(600, pawns=idle), st(5200, pawns=work), st(5500, pawns=work)])
    r = m.report()
    assert r["work"]["idle_streak_max_s"] == 10.0 and r["work"]["idle_streaks"] == 2
    assert r["extra"]["empty_game_hours"] == 1            # час 0 без работы, час 2 с работой


def test_losses_zero_one_two():
    a, b, c = pawn("a"), pawn("b"), pawn("c")
    m0 = run([st(0, pawns=[a, b, c]), st(300, pawns=[a, b, c])])
    assert crit(m0, "losses")["status"] == "PASS"
    died = {"type": "pawn_died", "tick": 400, "data": {"pawn_id": "c", "name": "C"}}
    m1 = run([st(0, pawns=[a, b, c]), st(300, pawns=[a, b], events=[died]), st(600, pawns=[a, b], events=[died])])
    assert len(m1.dead) == 1 and crit(m1, "losses")["status"] == "PASS"
    m2 = run([st(0, pawns=[a, b, c]), st(300, pawns=[a], events=[died])])           # c погиб по событию, b пропал из списка
    assert len(m2._lost()) == 2 and crit(m2, "losses")["status"] == "FAIL"
    m3 = run([st(0, pawns=[a, b, c]), st(300, pawns=[a, b, c]), st(600, pawns=[a, b, c])], cfg=cm.Config(max_losses=0))
    assert crit(m3, "losses")["status"] == "PASS"


def test_prisoner_leaving_and_broken_pawn_list_are_not_losses():
    m = run([st(0, pawns=[pawn("a"), pawn("x", prisoner=True)]), st(300, pawns=[pawn("a")])])
    assert not m._lost()
    s = st(600)
    s["colony"]["pawns"] = None                  # неполные данные
    m.update(s, 100)
    assert not m._lost()


def test_colony_died_event():
    m = run([st(0), st(300, events=[{"type": "colony_died", "tick": 1, "data": {}}])])
    assert m.colony_died and crit(m, "losses")["status"] == "FAIL"


def test_health_violation_and_breaks():
    ok = [pawn("a"), pawn("b")]
    bad = [pawn("a"), pawn("b", mood=0.1)]               # половина в норме: нарушения нет
    worse = [pawn("a", hp=0.3), pawn("b", mood=0.1)]     # в норме 0 из 2
    m = run([st(0, pawns=ok), st(300, pawns=ok), st(600, pawns=bad)])
    assert m.health_violations == 0 and crit(m, "health")["status"] == "PASS" and m.breaks_estimated == 1
    m2 = run([st(0, pawns=ok), st(300, pawns=worse), st(600, pawns=ok)])
    assert m2.health_violations == 1 and crit(m2, "health")["status"] == "FAIL"
    m3 = run([st(0, pawns=ok), st(300, pawns=worse)], cfg=cm.Config(max_health_violations=1))
    assert crit(m3, "health")["status"] == "PASS"
    m4 = run([st(0, pawns=ok), st(300, pawns=[pawn("a", mental_state="Berserk"), pawn("b", downed=True)])])
    assert m4.breaks_flagged == 1 and m4.health_violations == 1


def test_break_counts_once_with_hysteresis():
    seq = [0.5, 0.15, 0.18, 0.22, 0.3, 0.1]
    m = run([st(i * 300, pawns=[pawn("a", mood=x)]) for i, x in enumerate(seq)])
    assert m.breaks_estimated == 2


def test_missing_fields_do_not_crash():
    m = cm.ColonyMetrics()
    m.update({"tick": 1, "colony": {"pawns": [{"id": "x"}]}}, 0)
    m.update({"tick": 301, "colony": {"pawns": [{"id": "x"}, {"name": "без id"}], "buildings": [{"def": "Wall"}]}, "threats": [{}]}, 5)
    m.update({"tick": 601, "colony": {}}, 10)
    assert m.active_seconds == 10
    assert isinstance(m.report()["criteria"], list)


def test_resources_food_buildings_in_report():
    m = run([st(0, food=9.0, buildings=[bld("Bed", 1, 1)]), st(300, food=2.5, buildings=[bld("Bed", 1, 1), bld("Bed", 2, 1)])])
    r = m.report()
    assert r["extra"]["food_days_min"] == 2.5 and r["extra"]["buildings"] == {"Bed": 2} and r["extra"]["resources"] == {"WoodLog": 50}


def good_run(cfg):
    m = cm.ColonyMetrics(cfg)
    furn = furnished()
    rooms = {(2, 2): probe_info(), (3, 3): probe_info()}
    m.update(st(0), 0, {})
    for i in range(1, 10):
        threats = [enemy()] if i in (3, 4) else []
        m.update(st(i * 300, threats=threats, buildings=furn if i >= 2 else []), i * 5, rooms)
    return m


def test_report_json_format_and_exit_codes():
    cfg = cm.Config(active_seconds=40)
    m = good_run(cfg)
    rep = m.report()
    json.dumps(rep, ensure_ascii=False)                    # сериализуется
    assert {"time", "criteria", "attacks", "work", "losses", "health", "extra", "config", "approximations"} <= set(rep)
    assert [c["id"] for c in rep["criteria"]] == ["building", "attack", "work", "losses", "stall", "health"]
    assert rep["reached"] and rep["passed"] and rep["exit_code"] == 0 and m.exit_code() == 0
    text = cm.format_report(rep)
    assert "ИТОГ: PASS" in text and "Приближения" in text
    # время не набрано: не 0
    short = good_run(cm.Config(active_seconds=4000))
    assert short.exit_code() == 1 and short.exit_code(aborted=True) == 2
    # провал критерия: 1
    bad = run([st(0), st(300), st(600)], cfg=cm.Config(active_seconds=5))
    assert bad.done and bad.exit_code() == 1 and "ИТОГ: FAIL" in cm.format_report(bad.report())


def test_format_instant():
    m = run([st(0), st(300)])
    assert "колонистов 2" in cm.format_instant(m.instant)
    assert "не загружена" in cm.format_instant({"loaded": False})
