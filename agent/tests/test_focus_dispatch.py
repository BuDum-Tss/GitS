"""Проверка ответа LLM-диспетчера фокуса (ТЗ 18, FOC-7): конечный список операций, отказ при нарушении, без исключений."""
import focus_dispatch as d
from gits_sdk import protocol as P

CTX = {
    "now": 1000.0,
    "events": {
        "fe-1": {"module": "montage", "status": "pending", "critical": False, "priority": 62.0},
        "fe-2": {"module": "rimworld", "status": "pending", "critical": True, "priority": 95.0},
        "fe-3": {"module": "telegram", "status": "deferred", "critical": False, "priority": 40.0},
        "fe-4": {"module": "montage", "status": "resolved", "critical": False, "priority": 30.0},
    },
    "modules": {
        "montage": {"policies": {"idle": "background", "working": "active"}},
        "rimworld": {"policies": {"afk": "background", "observe": "active"}},
    },
    "holder": {"module": "rimworld", "interruptible": True},
    "checkpoints": {"t-1": {"status": "paused", "subgoals": 0}, "t-2": {"status": "resumed", "subgoals": 0}},
}


def ok_op(**op):
    return {"reason": "решение", "operations": [op]}


def test_non_object_response_is_rejected_and_nothing_applied():
    ops, rejected, reason = d.validate("просто текст", CTX)
    assert ops == [] and rejected == ["ответ не объект"] and reason == ""


def test_missing_operations_list():
    ops, rejected, _ = d.validate({"reason": "x", "operations": "defer"}, CTX)
    assert ops == [] and rejected == ["operations: не список"]


def test_unknown_operation_type_is_rejected():
    ops, rejected, _ = d.validate(ok_op(type="kill_module", module="montage"), CTX)
    assert ops == [] and "неизвестная операция" in rejected[0]


def test_valid_reprioritize_is_cleaned_and_bounded():
    ops, rejected, _ = d.validate(ok_op(type="reprioritize_event", event_id="fe-1", priority=45, extra="x"), CTX)
    assert rejected == [] and ops == [{"type": "reprioritize_event", "event_id": "fe-1", "priority": 45.0}]
    # шаг больше 25 не принимается
    ops, rejected, _ = d.validate(ok_op(type="reprioritize_event", event_id="fe-1", priority=95), CTX)
    assert ops == [] and "больше" in rejected[0]


def test_critical_event_cannot_be_reprioritized_or_deferred():
    _, rej, _ = d.validate(ok_op(type="reprioritize_event", event_id="fe-2", priority=50), CTX)
    assert rej and "критическ" in rej[0]
    _, rej, _ = d.validate(ok_op(type="defer_event", event_id="fe-2",
                                 resume_condition={"type": "context_changed"}), CTX)
    assert rej and "критическое" in rej[0]


def test_event_must_be_known_and_waiting():
    _, rej, _ = d.validate(ok_op(type="select_task", event_id="fe-404"), CTX)
    assert rej and "нет события" in rej[0]
    _, rej, _ = d.validate(ok_op(type="select_task", event_id="fe-4"), CTX)
    assert rej and "не ожидает" in rej[0]


def test_defer_accepts_known_resume_types_only():
    ops, rej, _ = d.validate(ok_op(type="defer_event", event_id="fe-1",
                                   resume_condition={"type": "context_changed"}), CTX)
    assert rej == [] and ops[0]["resume_condition"] == {"type": "context_changed"}
    _, rej, _ = d.validate(ok_op(type="defer_event", event_id="fe-1",
                                 resume_condition={"type": "forever"}), CTX)
    assert rej and "resume_condition" in rej[0]


def test_defer_time_must_be_near():
    _, rej, _ = d.validate(ok_op(type="defer_event", event_id="fe-1",
                                 resume_condition={"type": "time", "at": 1000.0 + 10_000}), CTX)
    assert rej and "полчаса" in rej[0]
    ops, rej, _ = d.validate(ok_op(type="defer_event", event_id="fe-1",
                                   resume_condition={"type": "time", "at": 1000.0 + 600}), CTX)
    assert rej == [] and ops[0]["resume_condition"]["at"] == 1600.0


def test_transition_needs_known_module_active_policy_and_pending_event():
    _, rej, _ = d.validate(ok_op(type="request_policy_transition", module="ghost", policy="observe"), CTX)
    assert rej and "модуля" in rej[0]
    _, rej, _ = d.validate(ok_op(type="request_policy_transition", module="montage", policy="idle"), CTX)
    assert rej and "не активная" in rej[0]
    ops, rej, _ = d.validate(ok_op(type="request_policy_transition", module="montage", policy="working"), CTX)
    assert rej == [] and ops == [{"type": "request_policy_transition", "module": "montage", "policy": "working"}]
    _, rej, _ = d.validate(ok_op(type="request_policy_transition", module="telegram", policy="x"), CTX)
    assert rej and "модуля" in rej[0]


def test_pause_only_holder_and_only_when_safe_and_no_critical():
    ops, rej, _ = d.validate(ok_op(type="pause_task", module="rimworld"), CTX)
    assert rej and "критическое" in rej[0]          # в очереди есть критическое событие
    ctx = {**CTX, "events": {"fe-1": CTX["events"]["fe-1"]}}
    ops, rej, _ = d.validate(ok_op(type="pause_task", module="rimworld"), ctx)
    assert rej == [] and ops == [{"type": "pause_task", "module": "rimworld"}]
    _, rej, _ = d.validate(ok_op(type="pause_task", module="montage"), ctx)
    assert rej and "не в фокусе" in rej[0]
    unsafe = {**ctx, "holder": {"module": "rimworld", "interruptible": False}}
    _, rej, _ = d.validate(ok_op(type="pause_task", module="rimworld"), unsafe)
    assert rej and "небезопасн" in rej[0]


def test_resume_only_paused_checkpoints():
    ops, rej, _ = d.validate(ok_op(type="resume_task", task_id="t-1"), CTX)
    assert rej == [] and ops == [{"type": "resume_task", "task_id": "t-1"}]
    _, rej, _ = d.validate(ok_op(type="resume_task", task_id="t-2"), CTX)
    assert rej and "не на паузе" in rej[0]
    _, rej, _ = d.validate(ok_op(type="resume_task", task_id="t-9"), CTX)
    assert rej and "нет задачи" in rej[0]


def test_subgoal_length_and_limit():
    ops, rej, _ = d.validate(ok_op(type="create_subgoal", task_id="t-1", goal="  собрать рендер  "), CTX)
    assert rej == [] and ops[0]["goal"] == "собрать рендер"
    _, rej, _ = d.validate(ok_op(type="create_subgoal", task_id="t-1", goal="x" * 300), CTX)
    assert rej and "goal" in rej[0]
    full = {**CTX, "checkpoints": {"t-1": {"status": "paused", "subgoals": 5}}}
    _, rej, _ = d.validate(ok_op(type="create_subgoal", task_id="t-1", goal="ещё"), full)
    assert rej and "максимум" in rej[0]


def test_too_many_operations_keeps_first_five():
    raw = {"reason": "много", "operations": [{"type": "select_task", "event_id": "fe-1"}] * 7}
    ops, rejected, _ = d.validate(raw, CTX)
    assert len(ops) == 5 and any("больше" in r for r in rejected)


def test_reason_is_trimmed_and_non_strings_are_dropped():
    ops, rej, reason = d.validate({"reason": "я" * 1000, "operations": []}, CTX)
    assert len(reason) == d.MAX_REASON and ops == [] and rej == []
    _, _, reason = d.validate({"reason": 42, "operations": []}, CTX)
    assert reason == ""


def test_operation_types_are_the_finite_list():
    assert set(d.OP_TYPES) == {"reprioritize_event", "defer_event", "select_task", "request_policy_transition",
                               "pause_task", "resume_task", "create_subgoal"}


def test_bool_is_not_a_number():
    _, rej, _ = d.validate(ok_op(type="reprioritize_event", event_id="fe-1", priority=True), CTX)
    assert rej and "priority" in rej[0]


def test_ruleset_consistent_with_sdk_policy_constants():
    assert set(P.POLICY_FOCUS) == {"active", "background"}
