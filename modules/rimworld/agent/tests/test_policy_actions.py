"""ТЗ 11e, POL-8: решения политик без игрового вызова: ступени скорости, срез и бюджет боя, застрявшие задачи, принуждение
и откат, выбор модели хода, условие запуска игры (POL-12)."""
import policy_actions as pa


def test_observe_speed_climbs_stepwise_in_quiet_and_is_capped_by_setting():
    ladder = [1, 2, 3]
    kw = dict(now=1000.0, quiet_since=0.0, last_step_at=0.0, ladder=ladder, step_quiet_s=120)
    assert pa.observe_speed_plan(current_speed=1, max_speed=3, **kw) == 2
    assert pa.observe_speed_plan(current_speed=2, max_speed=3, **{**kw, "last_step_at": 900.0}) is None   # ступень не чаще раза в 120 с
    assert pa.observe_speed_plan(current_speed=3, max_speed=3, **kw) is None                              # максимум
    assert pa.observe_speed_plan(current_speed=1, max_speed=1, **kw) is None                              # настройка ограничила 1


def test_observe_speed_waits_for_quiet_period():
    assert pa.observe_speed_plan(now=100.0, quiet_since=50.0, last_step_at=0.0, current_speed=1, ladder=[1, 2, 3],
                                 max_speed=3, step_quiet_s=120) is None


def test_next_speed_recovers_when_current_is_not_on_the_ladder():
    assert pa.next_speed(2, [1, 2, 3], 2) == 2 and pa.next_speed(5, [1, 2, 3], 3) == 1


def test_combat_slice_is_game_ticks_with_a_wall_clock_fallback_for_pause():
    assert pa.combat_ready(tick=1000, last_turn_tick=400, slice_ticks=600, now=10, last_turn_at=9) is True
    assert pa.combat_ready(tick=900, last_turn_tick=400, slice_ticks=600, now=10, last_turn_at=9) is False
    # игра стоит на паузе (тики не идут): через fallback_s по часам
    assert pa.combat_ready(tick=400, last_turn_tick=400, slice_ticks=600, now=40, last_turn_at=9) is True
    assert pa.combat_ready(tick=None, last_turn_tick=None, slice_ticks=600, now=1, last_turn_at=0.5) is False


def test_fight_budget_limits_per_fight_and_per_minute():
    clock = [0.0]
    b = pa.FightBudget(max_turns=3, max_per_min=2, clock=lambda: clock[0])
    assert b.allow()[0] and b.spend() is None
    assert b.allow()[0]
    b.spend()
    ok, why = b.allow()
    assert not ok and "в минуту" in why                                       # два хода за минуту уже есть
    clock[0] = 61.0
    assert b.allow()[0]                                                      # окно минуты сдвинулось
    b.spend()                                                                # третий ход боя
    ok, why = b.allow()
    assert ok is False and "бюджет боя" in why                               # max_turns исчерпан
    b.reset()
    assert b.allow()[0] and b.turns == 0


def test_mixed_model_thinks_with_claude_on_first_and_every_nth_turn():
    assert pa.use_local_model("mixed", 0, claude_every=3) is False
    assert pa.use_local_model("mixed", 1, claude_every=3) is True
    assert pa.use_local_model("mixed", 3, claude_every=3) is False
    assert pa.use_local_model("claude", 1) is False
    assert pa.use_local_model("local", 0) is True
    assert pa.use_local_model("claude", 0, claude_blocked=True) is True     # лимит Claude: только локально
    assert pa.use_local_model("none", 0) is True


def test_stalled_tasks_counted_in_game_hours_and_done_ones_forgotten():
    seen: dict[str, int] = {}
    tasks = [{"description": "стена у реки", "status": "in_progress", "x": 10, "z": 12},
             {"description": "сделано", "status": "done"}]
    assert pa.stalls(tasks, seen, tick=0, stall_hours=2) == []               # первое появление запоминается
    assert seen == {"стена у реки": 0}
    late = pa.stalls(tasks, seen, tick=2 * pa.TICKS_PER_HOUR, stall_hours=2)
    assert [t["description"] for t in late] == ["стена у реки"]
    assert pa.stalls([{"description": "стена у реки", "status": "done"}], seen, tick=3 * pa.TICKS_PER_HOUR, stall_hours=2) == []
    assert seen == {}                                                        # выполненную забыли


def test_forced_work_needs_a_cell_idle_pawn_and_respects_the_interval():
    stalled = [{"description": "стена", "status": "pending"},
               {"description": "дерево", "status": "pending", "x": 5, "z": 6, "pawn_id": "p1", "work": "PlantCutting"}]
    acts = pa.plan_forced(stalled, forced={}, idle_pawn_ids=["p9"], tick=0, interval_hours=2, confirm=False)
    assert acts[0]["type"] == "note" and "нет клетки" in acts[0]["text"]                      # без клетки не принуждаем
    force = acts[1]
    assert force["type"] == "force" and force["pawn"] == "p1"
    assert force["cmd"] == {"type": "force_work", "pawn_id": "p1", "targets": [{"x": 5, "z": 6}], "queue": False,
                            "work": "PlantCutting"}
    # тот же порядок не чаще раза за интервал
    again = pa.plan_forced(stalled[1:], forced={"дерево": {"pawn": "p1", "tick": 0}}, idle_pawn_ids=["p9"],
                           tick=pa.TICKS_PER_HOUR, interval_hours=2, confirm=False)
    assert again == []
    later = pa.plan_forced(stalled[1:], forced={"дерево": {"pawn": "p1", "tick": 0}}, idle_pawn_ids=["p9"],
                           tick=3 * pa.TICKS_PER_HOUR, interval_hours=2, confirm=False)
    assert later and later[0]["type"] == "force"


def test_no_idle_pawn_means_a_note_not_a_command():
    acts = pa.plan_forced([{"description": "дерево", "x": 1, "z": 1}], forced={}, idle_pawn_ids=[], tick=0,
                          interval_hours=2, confirm=False)
    assert acts == [{"type": "note", "task": "дерево", "text": "некого назначить: нет свободного поселенца"}]


def test_confirm_mode_only_proposes():
    acts = pa.plan_forced([{"description": "дерево", "x": 1, "z": 1}], forced={}, idle_pawn_ids=["p1"], tick=0,
                          interval_hours=2, confirm=True)
    assert [a["type"] for a in acts] == ["note"] and "подтверждение" in acts[0]["text"]


def test_rollback_after_limit_and_forget_when_done():
    forced = {"дерево": {"pawn": "p1", "tick": 0}, "стена": {"pawn": "p2", "tick": 0}}
    assert pa.plan_rollback(forced, done={"стена"}, tick=0, rollback_hours=1) == [{"type": "forget", "task": "стена"}]
    rb = pa.plan_rollback(forced, done=set(), tick=pa.TICKS_PER_HOUR, rollback_hours=1)
    rolled = [a for a in rb if a["type"] == "rollback"]
    assert rolled and rolled[0]["cmd"] == {"type": "clear_work_queue", "pawn_id": "p1", "stop_current": False}
    assert pa.plan_rollback(forced, done=set(), tick=pa.TICKS_PER_HOUR - 1, rollback_hours=1) == []


def test_module_launches_game_only_in_focus_with_setting_or_after_crash():
    assert pa.launch_allowed(in_focus=False, autostart=True, seen_running_in_focus=True) is False     # вне фокуса не трогаем
    assert pa.launch_allowed(in_focus=True, autostart=False, seen_running_in_focus=False) is False    # по умолчанию нет
    assert pa.launch_allowed(in_focus=True, autostart=True, seen_running_in_focus=False) is True      # настройка включена
    assert pa.launch_allowed(in_focus=True, autostart=False, seen_running_in_focus=True) is True      # упала при фокусе
