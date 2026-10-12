"""Ответ LLM-диспетчера фокуса (ТЗ 18, FOC-7): разбор и проверка по конечному списку операций.

Модель не вызывает методы модулей. Она предлагает операции из OP_TYPES; каждая проверяется здесь по контексту (известные
ids событий, задач, модулей и политик, текущий фокус). Невалидная операция отклоняется с причиной, остальные применяет
менеджер фокуса (focus.py) теми же правилами, что и детерминированный путь. Контекст строится из структурированного
состояния (очередь, фокус, политики, checkpoint), а не из полного снимка мира."""
from __future__ import annotations

from typing import Any

OP_TYPES = ("reprioritize_event", "defer_event", "select_task", "request_policy_transition", "pause_task",
            "resume_task", "create_subgoal")
RESUME_TYPES = ("context_changed", "time", "dnd_off", "unpin")     # условия возобновления отложенного события
MAX_OPS = 5
MAX_REASON = 300
MAX_GOAL = 200
MAX_SUBGOALS = 5
MAX_PRIORITY_STEP = 25        # диспетчер не сдвигает приоритет события больше чем на эту величину
DEFER_MAX_S = 1800.0          # отложить можно не дальше чем на полчаса вперёд
PENDING = "pending"
DEFERRED = "deferred"


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _event_ref(op: dict, ctx: dict, statuses: tuple[str, ...]) -> str | None:
    """Ошибка, если event_id не из очереди или событие в неподходящем состоянии."""
    eid = op.get("event_id")
    if not isinstance(eid, str) or eid not in ctx["events"]:
        return f"нет события «{eid}» в очереди"
    if ctx["events"][eid]["status"] not in statuses:
        return f"событие «{eid}» уже не ожидает решения"
    return None


def _check_reprioritize(op: dict, ctx: dict) -> str | None:
    err = _event_ref(op, ctx, (PENDING, DEFERRED))
    if err:
        return err
    ev = ctx["events"][op["event_id"]]
    if ev["critical"]:
        return "критическое событие не понижается и не повышается диспетчером"
    new = op.get("priority")
    if not _is_num(new) or not 0 <= new <= 100:
        return "priority: число от 0 до 100"
    if abs(new - ev["priority"]) > MAX_PRIORITY_STEP:
        return f"изменение приоритета больше {MAX_PRIORITY_STEP} не принимается"
    return None


def _check_defer(op: dict, ctx: dict) -> str | None:
    err = _event_ref(op, ctx, (PENDING,))
    if err:
        return err
    if ctx["events"][op["event_id"]]["critical"]:
        return "критическое событие нельзя отложить"
    cond = op.get("resume_condition")
    if not isinstance(cond, dict) or cond.get("type") not in RESUME_TYPES:
        return f"resume_condition.type: одно из {list(RESUME_TYPES)}"
    if cond["type"] == "time":
        at = cond.get("at")
        if not _is_num(at) or not ctx["now"] < at <= ctx["now"] + DEFER_MAX_S:
            return "resume_condition.at: время не дальше полчаса вперёд"
    return None


def _check_select(op: dict, ctx: dict) -> str | None:
    return _event_ref(op, ctx, (PENDING,))


def _check_transition(op: dict, ctx: dict) -> str | None:
    module, policy = op.get("module"), op.get("policy")
    mod = ctx["modules"].get(module)
    if mod is None:
        return f"модуля «{module}» нет среди подключённых"
    if mod["policies"].get(policy) != "active":
        return f"политики «{policy}» нет у модуля «{module}» или она не активная"
    if not any(e["module"] == module and e["status"] == PENDING for e in ctx["events"].values()):
        return f"у модуля «{module}» нет ожидающего события"
    return None


def _check_pause(op: dict, ctx: dict) -> str | None:
    holder = ctx.get("holder")
    if holder is None or holder["module"] != op.get("module"):
        return f"модуль «{op.get('module')}» сейчас не в фокусе"
    if not holder.get("interruptible", True):
        return "модуль сообщил, что сейчас небезопасная точка"
    if any(e["critical"] and e["status"] == PENDING for e in ctx["events"].values()):
        return "есть критическое событие: пауза не нужна"
    return None


def _check_resume(op: dict, ctx: dict) -> str | None:
    cp = ctx["checkpoints"].get(op.get("task_id"))
    if cp is None:
        return f"нет задачи «{op.get('task_id')}»"
    if cp["status"] != "paused":
        return f"задача «{op.get('task_id')}» не на паузе"
    return None


def _check_subgoal(op: dict, ctx: dict) -> str | None:
    cp = ctx["checkpoints"].get(op.get("task_id"))
    if cp is None:
        return f"нет задачи «{op.get('task_id')}»"
    goal = op.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL:
        return f"goal: строка от 1 до {MAX_GOAL} символов"
    if cp.get("subgoals", 0) >= MAX_SUBGOALS:
        return "у задачи уже максимум подцелей"
    return None


_CHECKERS = {
    "reprioritize_event": _check_reprioritize,
    "defer_event": _check_defer,
    "select_task": _check_select,
    "request_policy_transition": _check_transition,
    "pause_task": _check_pause,
    "resume_task": _check_resume,
    "create_subgoal": _check_subgoal,
}


def _clean(op: dict) -> dict:
    """Только известные поля операции (лишнее из ответа модели не проходит дальше)."""
    keep = {
        "reprioritize_event": ("event_id", "priority"),
        "defer_event": ("event_id", "resume_condition"),
        "select_task": ("event_id",),
        "request_policy_transition": ("module", "policy"),
        "pause_task": ("module",),
        "resume_task": ("task_id",),
        "create_subgoal": ("task_id", "goal"),
    }[op["type"]]
    out = {"type": op["type"]}
    for key in keep:
        if key in op:
            out[key] = op[key]
    if op["type"] == "reprioritize_event":
        out["priority"] = float(op["priority"])
    if op["type"] == "defer_event":
        cond = op["resume_condition"]
        out["resume_condition"] = {k: v for k, v in cond.items() if k in ("type", "at", "module")}
    if op["type"] == "create_subgoal":
        out["goal"] = op["goal"].strip()[:MAX_GOAL]
    return out


def validate(raw: Any, ctx: dict) -> tuple[list[dict], list[str], str]:
    """Проверить ответ модели. Возвращает (принятые операции, причины отказов, причина решения от модели).

    ctx: events {id: {module, status, critical, priority}}, modules {id: {policies: {policy: focus}}}, holder
    {module, interruptible} или None, checkpoints {task_id: {status, subgoals}}, now (время). Ответ не объект или
    без списка operations даёт пустой результат: тогда работают детерминированные правила."""
    if not isinstance(raw, dict):
        return [], ["ответ не объект"], ""
    reason = raw.get("reason")
    reason = reason.strip()[:MAX_REASON] if isinstance(reason, str) else ""
    ops_raw = raw.get("operations")
    if not isinstance(ops_raw, list):
        return [], ["operations: не список"], reason
    rejected: list[str] = []
    if len(ops_raw) > MAX_OPS:
        rejected.append(f"операций больше {MAX_OPS}: лишние отброшены")
    ops: list[dict] = []
    for i, op in enumerate(ops_raw[:MAX_OPS]):
        if not isinstance(op, dict):
            rejected.append(f"{i}: операция не объект")
            continue
        typ = op.get("type")
        if typ not in OP_TYPES:
            rejected.append(f"{i}: неизвестная операция {typ!r}")
            continue
        err = _CHECKERS[typ](op, ctx)
        if err:
            rejected.append(f"{i} {typ}: {err}")
            continue
        ops.append(_clean(op))
    return ops, rejected, reason
