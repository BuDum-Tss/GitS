"""Общие задачи (ТЗ 32, раздел 4.4, WS-8). Чистые правила без ввода-вывода, настроек, часов и asyncio.

Задача это единица работы в общем дереве: цель, подзадачи, шаги, статус; принадлежит среде (workspace). Планирование универсально и
не зависит от среды: среда поставляет инструменты, `done_when` и безопасные точки, а дерево задач ведёт ядро. Напоминание себе и
план пользователя тоже задачи (`kind`), у которых есть срок (`due`). Время и идентификаторы приходят аргументами.
"""
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# статусы
PENDING, ACTIVE, WAITING, BLOCKED = "pending", "active", "waiting", "blocked"
DONE, FAILED, CANCELLED = "done", "failed", "cancelled"
STATUSES = (PENDING, ACTIVE, WAITING, BLOCKED, DONE, FAILED, CANCELLED)
TERMINAL = (DONE, FAILED, CANCELLED)
OPEN = (PENDING, ACTIVE, WAITING, BLOCKED)

# происхождение
USER, SELF, MODULE = "user", "self", "module"
ORIGINS = (USER, SELF, MODULE)

# вид задачи: обычная, напоминание себе (срок -> событие), план пользователя (зеркало записи памяти)
KIND_TASK, KIND_REMINDER, KIND_PLAN = "task", "reminder", "plan"
KINDS = (KIND_TASK, KIND_REMINDER, KIND_PLAN)

# статус шага
STEP_PENDING, STEP_DONE, STEP_SKIPPED = "pending", "done", "skipped"

GOAL_MAX = 500
STEPS_MAX = 12
PRIORITY_MIN, PRIORITY_MAX, PRIORITY_DEFAULT = 1, 5, 3
REMINDER_TTL_S = 7 * 24 * 3600.0           # напоминание старше срока на неделю уже не нужно


@dataclass(frozen=True)
class Step:
    text: str
    status: str = STEP_PENDING

    def to_dict(self) -> dict:
        return {"text": self.text, "status": self.status}

    @staticmethod
    def from_dict(d: Any) -> "Step":
        if isinstance(d, str):
            return Step(d.strip()[:GOAL_MAX])
        status = str(d.get("status") or STEP_PENDING)
        return Step(str(d.get("text") or "").strip()[:GOAL_MAX], status if status in (STEP_PENDING, STEP_DONE, STEP_SKIPPED) else STEP_PENDING)


@dataclass(frozen=True)
class Task:
    id: str
    workspace: str
    goal: str
    parent: str | None = None
    steps: tuple[Step, ...] = ()
    status: str = PENDING
    origin: str = USER
    priority: int = PRIORITY_DEFAULT
    due: float | None = None
    created: float = 0.0
    updated: float = 0.0
    checkpoint: dict | None = None             # состояние при уходе из среды: что сделано, что дальше
    kind: str = KIND_TASK
    meta: dict = field(default_factory=dict)   # источник, глубина цепочки, недоверенность (напоминание), plan_id (план)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN

    def to_dict(self) -> dict:
        return {"id": self.id, "workspace": self.workspace, "goal": self.goal, "parent": self.parent,
                "steps": [s.to_dict() for s in self.steps], "status": self.status, "origin": self.origin,
                "priority": self.priority, "due": self.due, "created": self.created, "updated": self.updated,
                "checkpoint": self.checkpoint, "kind": self.kind, "meta": dict(self.meta)}

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(id=str(d["id"]), workspace=str(d.get("workspace") or "assistant"), goal=str(d.get("goal") or ""),
                    parent=d.get("parent") or None, steps=tuple(Step.from_dict(s) for s in (d.get("steps") or [])),
                    status=d.get("status") if d.get("status") in STATUSES else PENDING,
                    origin=d.get("origin") if d.get("origin") in ORIGINS else USER,
                    priority=clamp_priority(d.get("priority")), due=float(d["due"]) if d.get("due") is not None else None,
                    created=float(d.get("created") or 0.0), updated=float(d.get("updated") or 0.0),
                    checkpoint=d.get("checkpoint") or None, kind=d.get("kind") if d.get("kind") in KINDS else KIND_TASK,
                    meta=dict(d.get("meta") or {}))


def clamp_priority(value: Any) -> int:
    try:
        return max(PRIORITY_MIN, min(PRIORITY_MAX, int(value)))
    except (TypeError, ValueError):
        return PRIORITY_DEFAULT


def make_steps(texts: Iterable[Any]) -> tuple[Step, ...]:
    """Шаги из списка текстов (или словарей Planner): пустые и дубли выброшены, не больше `STEPS_MAX`."""
    out: list[Step] = []
    for t in texts:
        step = Step.from_dict(t) if isinstance(t, (dict, str)) else Step(str(t).strip()[:GOAL_MAX])
        if step.text and all(step.text.lower() != s.text.lower() for s in out):
            out.append(step)
    return tuple(out[:STEPS_MAX])


def new_task(task_id: str, workspace: str, goal: str, now: float, *, origin: str = USER, parent: str | None = None,
             priority: int = PRIORITY_DEFAULT, due: float | None = None, steps: Iterable[Any] = (), kind: str = KIND_TASK,
             meta: dict | None = None) -> Task:
    return Task(id=task_id, workspace=workspace, goal=str(goal).strip()[:GOAL_MAX], parent=parent, steps=make_steps(steps),
                origin=origin if origin in ORIGINS else USER, priority=clamp_priority(priority), due=due, created=now,
                updated=now, kind=kind if kind in KINDS else KIND_TASK, meta=dict(meta or {}))


# ── переходы ─────────────────────────────────────────────────────────────────

def can_transition(old: str, new: str) -> bool:
    """Из конечного статуса (done, failed, cancelled) задача не возвращается; остальное свободно."""
    return old not in TERMINAL and new in STATUSES


def with_status(task: Task, status: str, now: float) -> Task:
    """Задача с новым статусом; недопустимый переход оставляет её как есть. Закрытая задача закрывает свои шаги не трогая."""
    if not can_transition(task.status, status) or status == task.status:
        return task
    return replace(task, status=status, updated=now)


# ── шаги ─────────────────────────────────────────────────────────────────────

def next_step(task: Task) -> Step | None:
    """Первый невыполненный шаг или None."""
    return next((s for s in task.steps if s.status == STEP_PENDING), None)


def next_step_index(task: Task) -> int | None:
    return next((i for i, s in enumerate(task.steps) if s.status == STEP_PENDING), None)


def finish_step(task: Task, index: int | None, now: float, status: str = STEP_DONE) -> Task:
    """Отметить шаг (по умолчанию текущий) выполненным; когда шагов не осталось, задача становится `done`."""
    idx = next_step_index(task) if index is None else index
    if idx is None or not (0 <= idx < len(task.steps)) or not task.is_open:
        return task
    steps = tuple(replace(s, status=status) if i == idx else s for i, s in enumerate(task.steps))
    out = replace(task, steps=steps, updated=now)
    return with_status(out, DONE, now) if all(s.status != STEP_PENDING for s in steps) else out


def progress(task: Task) -> tuple[int, int]:
    """(выполнено, всего) по шагам."""
    return sum(1 for s in task.steps if s.status != STEP_PENDING), len(task.steps)


# ── дерево ───────────────────────────────────────────────────────────────────

def parent_status(current: str, children: Iterable[str]) -> str:
    """Статус родителя по статусам подзадач. Без подзадач и у закрытого родителя остаётся прежний.
    Все закрыты: failed, если была неудача, иначе done (хоть одна выполнена), иначе cancelled. Что-то открыто: active > waiting >
    blocked (когда больше нечего делать) > pending."""
    kids = list(children)
    if not kids or current in TERMINAL:
        return current
    open_ = [k for k in kids if k in OPEN]
    if not open_:
        if FAILED in kids:
            return FAILED
        return DONE if DONE in kids else CANCELLED
    if ACTIVE in open_:
        return ACTIVE
    if WAITING in open_:
        return WAITING
    if all(k == BLOCKED for k in open_):
        return BLOCKED
    return PENDING


def children_of(tasks: Iterable[Task], parent_id: str) -> list[Task]:
    return [t for t in tasks if t.parent == parent_id]


def recompute_parent(parent: Task, children: Iterable[Task], now: float) -> Task:
    """Родитель с пересчитанным статусом (поднимается выше цепочкой вызовов сервиса)."""
    status = parent_status(parent.status, [c.status for c in children])
    return with_status(parent, status, now) if status != parent.status else parent


def descendants(tasks: Iterable[Task], root_id: str) -> list[Task]:
    """Все подзадачи на любую глубину (без самого корня); циклы по `parent` не зацикливают обход."""
    by_parent: dict[str | None, list[Task]] = {}
    for t in tasks:
        by_parent.setdefault(t.parent, []).append(t)
    out, seen, stack = [], {root_id}, [root_id]
    while stack:
        for c in by_parent.get(stack.pop(), []):
            if c.id not in seen:
                seen.add(c.id)
                out.append(c)
                stack.append(c.id)
    return out


# ── выбор и прерывание ───────────────────────────────────────────────────────

def pick_active(tasks: Iterable[Task], workspace: str) -> Task | None:
    """Активная задача среды: открытая, не напоминание, без открытых подзадач (работают листья). Уже активная раньше, потом
    выше приоритет, ближе срок, старше. Ожидающие и заблокированные не выбираются."""
    items = list(tasks)
    parents_open = {t.parent for t in items if t.is_open and t.parent}
    cands = [t for t in items if t.workspace == workspace and t.status in (ACTIVE, PENDING) and t.kind != KIND_REMINDER
             and t.id not in parents_open]
    if not cands:
        return None
    return min(cands, key=lambda t: (t.status != ACTIVE, -t.priority, t.due if t.due is not None else float("inf"), t.created))


def can_interrupt(task: Task | None, safe_point: bool = False) -> bool:
    """Можно ли прервать работу над задачей сейчас: не идущую (нет, ждёт, закрыта) можно всегда, идущую только в безопасной точке."""
    return task is None or task.status != ACTIVE or bool(safe_point)


# ── сроки ────────────────────────────────────────────────────────────────────

def due_for_nudge(tasks: Iterable[Task], now: float, lead_s: float = 0.0, ttl_s: float = REMINDER_TTL_S) -> list[Task]:
    """Задачи, о которых пора напомнить: открытые (pending, waiting), со сроком, до которого осталось не больше `lead_s`
    (или он прошёл не дольше `ttl_s` назад). Порядок по сроку."""
    out = [t for t in tasks if t.status in (PENDING, WAITING) and t.due is not None and now >= t.due - lead_s and now - t.due <= ttl_s]
    return sorted(out, key=lambda t: (t.due, t.created))


def next_due(tasks: Iterable[Task]) -> float | None:
    """Ближайший срок среди открытых задач со сроком (для таймера)."""
    dues = [t.due for t in tasks if t.status in (PENDING, WAITING) and t.due is not None]
    return min(dues) if dues else None


def checkpoint_of(tasks: Iterable[Task], workspace: str, goal: str, now: float) -> dict:
    """Дерево задач среды для checkpoint при уходе: открытые задачи с шагами и прогрессом (ТЗ 32, WS-8)."""
    open_tasks = [t for t in tasks if t.workspace == workspace and t.is_open and t.kind != KIND_REMINDER]
    return {"workspace": workspace, "goal": goal, "ts": now,
            "tasks": [{"id": t.id, "goal": t.goal, "parent": t.parent, "status": t.status, "steps": [s.to_dict() for s in t.steps]}
                      for t in open_tasks]}
