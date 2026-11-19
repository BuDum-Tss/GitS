"""Рефлекс настроения: чистая plan_morale (расписание, отдых при низком настроении, предупреждение ядру) без игры и сети."""
import morale_keeper as mk
from morale_keeper import Memory, plan_morale

DEFAULT = ["Sleep"] * 6 + ["Anything"] * 16 + ["Sleep"] * 2          # как у новой игры: сон 22-5
ALL_WORK = ["Work"] * 24


def pawn(pid, mood=0.7, schedule=None, **kw):
    base = {"id": pid, "name": f"К{pid}", "mood_pct": mood, "schedule": list(schedule or DEFAULT), "is_prisoner": False,
            "is_downed": False, "is_drafted": False, "mood_thoughts": []}
    base.update(kw)
    return base


def st(pawns, tick=100000, hour=10):
    return {"tick": tick, "hour": hour, "colony": {"pawns": list(pawns)}, "threats": []}


def sets(plan):
    return {(c["pawn_id"], c["hour"], c["schedule_type"]) for c in plan.actions if c["type"] == "set_schedule"}


def test_template_detection():
    assert mk.is_template_schedule(DEFAULT) and mk.is_template_schedule(ALL_WORK)
    assert not mk.is_template_schedule(["Sleep"] * 6 + ["Anything"] * 14 + ["Joy"] * 2 + ["Sleep"] * 2)   # есть Joy
    assert not mk.is_template_schedule(["Meditate"] + ["Anything"] * 23)
    assert not mk.is_template_schedule(["Anything"] * 12 + ["Sleep"] * 12)            # сон днём: настраивали руками
    assert not mk.is_template_schedule([])


def test_template_schedule_gets_sleep_and_joy_hours_without_window():
    plan = plan_morale(st([pawn("a", schedule=ALL_WORK)]), 100, Memory())
    got = sets(plan)
    assert {("a", h, "Sleep") for h in (22, 23, 0, 1, 2, 3, 4, 5)} <= got and {("a", 20, "Joy"), ("a", 21, "Joy")} <= got
    assert all(c["reflex"] is True for c in plan.actions)                               # правка без открытого окна


def test_default_game_schedule_only_gets_joy():
    plan = plan_morale(st([pawn("a")]), 100, Memory())
    assert sets(plan) == {("a", 20, "Joy"), ("a", 21, "Joy")}


def test_agent_configured_schedule_is_not_overwritten():
    custom = ["Sleep"] * 6 + ["Work"] * 10 + ["Joy"] * 4 + ["Anything"] * 2 + ["Sleep"] * 2
    plan = plan_morale(st([pawn("a", schedule=custom)]), 100, Memory())
    assert plan.actions == []


def test_agent_recent_schedule_tool_stops_everything_then_resumes():
    state = st([pawn("a", mood=0.2)])
    assert plan_morale(state, 100, Memory(), agent_schedule_at=50).actions == []        # правил 50 с назад
    assert plan_morale(state, 700, Memory(), agent_schedule_at=50).actions               # прошло больше 600 с


def test_runs_once_per_interval_and_retries_slowly():
    mem, state = Memory(), st([pawn("a")])
    assert plan_morale(state, 100, mem).actions
    assert plan_morale(state, 120, mem).actions == []                                    # раньше 30 с: пусто
    assert plan_morale(state, 131, mem).actions == []                                    # расписание повторяем не раньше 60 с
    assert plan_morale(state, 162, mem).actions                                          # игра не приняла: пробуем снова


def test_attempts_are_limited():
    mem, state = Memory(), st([pawn("a")])
    for i in range(6):
        plan_morale(state, 100 + 100 * i, mem)
    assert mem.attempts["a"][0] == mk.MAX_ATTEMPTS


def test_low_mood_gets_joy_for_next_hours_then_schedule_returns():
    mem = Memory()
    sched = ["Sleep"] * 6 + ["Work"] * 10 + ["Joy"] * 2 + ["Work"] * 2 + ["Sleep"] * 4        # не шаблон: свои Joy
    plan = plan_morale(st([pawn("a", mood=0.3, schedule=sched)], tick=100000, hour=10), 100, mem)
    assert sets(plan) == {("a", 10, "Joy"), ("a", 11, "Joy"), ("a", 12, "Joy")}
    assert "настроение 30%" in plan.notes[0]
    now_sched = list(sched)
    for h in (10, 11, 12):
        now_sched[h] = "Joy"
    back = plan_morale(st([pawn("a", mood=0.6, schedule=now_sched)], tick=100000 + 3 * 2500, hour=13), 200, mem)
    assert sets(back) == {("a", 10, "Work"), ("a", 11, "Work"), ("a", 12, "Work")}     # прежнее расписание обратно
    assert "a" not in mem.temp


def test_low_mood_skips_drafted_mental_and_cooldown_and_unknown_hour():
    sched = ["Sleep"] * 6 + ["Work"] * 10 + ["Joy"] * 2 + ["Work"] * 2 + ["Sleep"] * 4
    pawns = [pawn("d", mood=0.2, is_drafted=True, schedule=sched), pawn("m", mood=0.2, mental_state="Wander_Sad", schedule=sched),
             pawn("ok", mood=0.5, schedule=sched)]
    assert plan_morale(st(pawns), 100, Memory()).actions == []
    mem = Memory()
    assert plan_morale(st([pawn("a", mood=0.2, schedule=sched)]), 100, mem).actions
    mem.temp.clear()
    assert plan_morale(st([pawn("a", mood=0.2, schedule=sched)], tick=100000 + 4 * 2500, hour=14), 200, mem).actions == []   # остывание
    no_hour = st([pawn("b", mood=0.2, schedule=sched)])
    del no_hour["hour"]
    assert plan_morale(no_hour, 100, Memory()).actions == []


# ── предупреждение ядру ───────────────────────────────────────────────────────

def thought(label, effect):
    return {"label": label, "effect": effect}


def test_warning_at_one_third_with_common_causes():
    pawns = [pawn("a", mood=0.3, mood_thoughts=[thought("Голод", -8), thought("Спал на земле", -4)]),
             pawn("b", mood=0.39, mood_thoughts=[thought("Голод", -6), thought("Тесно", -2)]),
             pawn("c", mood=0.8), pawn("d", mood=0.7), pawn("e", mood=0.9), pawn("f", mood=0.6)]
    plan = plan_morale(st(pawns), 100, Memory())
    w = plan.warning
    assert w["urgency"] == 3 and w["text"].startswith("У 2 из 6 колонистов настроение ниже 0,4: Голод (у 2)")
    assert w["data"]["low"] == ["Кa", "Кb"] and w["data"]["causes"][0] == {"thought": "Голод", "pawns": 2, "effect": -14.0}


def test_no_warning_below_threshold_or_at_exact_boundary():
    pawns = [pawn("a", mood=0.2)] + [pawn(c, mood=0.8) for c in "bcd"]                   # 1 из 4: меньше трети
    assert plan_morale(st(pawns), 100, Memory()).warning is None
    pawns = [pawn("a", mood=0.4)] + [pawn(c, mood=0.8) for c in "bc"]                    # ровно 0,4 не ниже 0,4
    assert plan_morale(st(pawns), 100, Memory()).warning is None
    three = [pawn("a", mood=0.2)] + [pawn(c, mood=0.8) for c in "bc"]                    # 1 из 3: ровно треть
    assert plan_morale(st(three), 100, Memory()).warning is not None


def test_warning_repeats_not_more_often_than_150s_and_prisoners_are_not_counted():
    mem = Memory()
    pawns = [pawn("a", mood=0.2), pawn("p", mood=0.1, is_prisoner=True)]
    assert plan_morale(st(pawns), 100, mem).warning["text"].startswith("У 1 из 1 ")
    assert plan_morale(st(pawns), 140, mem).warning is None
    assert plan_morale(st(pawns), 260, mem).warning is not None


def test_disabled_gives_empty_plan():
    mem = Memory()
    off = plan_morale(st([pawn("a", mood=0.1)]), 100, mem, enabled=False)
    assert off.actions == [] and off.warning is None and mem.last_run == 0.0
