"""ТЗ 11e, POL-4/POL-5/POL-11/POL-14: машина состояний политик (приоритет, дребезг, просьбы, afk, журнал, файл запроса)."""
import json

import policies
import policy_manager as pm


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make():
    clock, logged = Clock(), []
    mgr = pm.PolicyManager(policies.get(), clock=clock, log=lambda k, t: logged.append((k, t)))
    return mgr, clock, logged


def test_starts_afk_and_events_do_not_enter_games_outside_focus():
    mgr, _, _ = make()
    assert mgr.current == "afk" and not mgr.in_focus
    assert mgr.on_event("raid", 5) is False and mgr.current == "afk"        # вне фокуса игровые события ничего не включают


def test_core_activation_leaves_afk_and_logs_the_transition():
    mgr, _, logged = make()
    assert mgr.leave_afk("observe", "фокус выдан ядром")
    assert mgr.current == "observe" and mgr.in_focus
    assert logged and "afk → observe" in logged[-1][1]
    assert mgr.history[-1]["source"] == "core"


def test_raid_preempts_at_once_and_exit_returns_to_observe():
    mgr, _, _ = make()
    mgr.leave_afk("observe", "")
    assert mgr.on_event("raid", 5, "набег")
    assert mgr.current == "combat"                                           # приоритет 4 прерывает наблюдение без ожидания
    assert mgr.finish("угроза миновала") and mgr.current == "observe"


def test_higher_priority_preempts_and_lower_priority_is_refused_during_dwell():
    mgr, clock, _ = make()
    mgr.leave_afk("observe", "")
    assert mgr.on_event("plan_request", 2) and mgr.current == "plan"         # наблюдение (1) -> планирование (2): сразу
    assert mgr.on_event("task_stalled", 3) and mgr.current == "manual"       # ручное (3) сильнее планирования (2): сразу
    assert mgr.on_event("plan_request", 2) is False                          # планирование не прерывает ручное
    clock.t += 61
    assert mgr.finish("ручные задачи обработаны") and mgr.current == "observe"


def test_rules_and_events_never_push_a_policy_down_only_finish_does():
    mgr, clock, _ = make()
    mgr.leave_afk("observe", "")
    mgr.on_event("plan_request", 2)
    assert mgr.dwell_left_s() == 60
    clock.t += 61
    assert mgr.rule_switch("observe", "") is False                           # правило не опускает планирование
    assert mgr.on_event("letter", 2) is False                                # событие ниже по приоритету тоже нет
    assert mgr.finish("план разложен") and mgr.current == "observe"          # а выход по условию возвращает в наблюдение


def test_model_request_only_within_switch_to_and_after_dwell():
    mgr, clock, _ = make()
    mgr.leave_afk("observe", "")
    assert mgr.request("combat", "хочу бой")[0] is False                     # в бой по просьбе нельзя
    assert mgr.request("plan", "разметить")[0] is True
    assert mgr.request("observe", "назад")[0] is False                       # у плана dwell 60 с ещё не прошло
    clock.t += 61
    assert mgr.request("observe", "назад")[0] is True
    assert mgr.request("nowhere", "")[0] is False


def test_rule_switch_does_not_preempt_higher_priority():
    mgr, _, _ = make()
    mgr.leave_afk("observe", "")
    assert mgr.rule_switch("plan", "правило: орошение") and mgr.current == "plan"
    mgr.on_event("raid", 5)
    assert mgr.rule_switch("plan", "правило") is False                       # бой сильнее плана


def test_enter_afk_on_focus_release_and_modes_fall_back_inside_policy():
    mgr, _, _ = make()
    mgr.leave_afk("observe", "")
    assert mgr.enter_afk("фокус у другой задачи") and mgr.current == "afk"
    assert mgr.enter_afk("ещё раз") is False                                 # уже вне фокуса
    mgr.leave_afk("observe", "")
    assert mgr.coerce_mode("build") == "build"                               # режим есть в наборе наблюдения
    assert mgr.coerce_mode("screen") == "observe"                            # режима нет в наборе: первый режим политики


def test_snapshot_shows_policy_reason_and_history():
    mgr, _, _ = make()
    mgr.leave_afk("observe", "фокус")
    mgr.on_event("raid", 5, "набег у реки")
    snap = mgr.snapshot()
    assert snap["id"] == "combat" and "набег" in snap["reason"] and snap["history"][-1]["to"] == "combat"


def test_file_requests_roundtrip_and_validate_names(tmp_path):
    path = tmp_path / "policy_request.json"
    assert pm.write_request("plan", "разметить комнату", path=path) is None
    assert json.loads(path.read_text(encoding="utf-8"))["policy"] == "plan"
    assert pm.take_request(path=path)["policy"] == "plan"
    assert pm.take_request(path=path) is None                                # одноразовая
    assert "нет политики" in pm.write_request("dance", "", path=path)
    assert "нельзя запросить" in pm.write_request("combat", "", path=path)   # бой только событиями
