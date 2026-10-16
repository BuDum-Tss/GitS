"""TaskService (ТЗ 32, WS-8): общие задачи и планирование для всех сред.

Создание задач и подзадач, разбиение цели на шаги через `Planner`, выбор активной задачи среды, checkpoint при уходе из среды и
возобновление, закрытие. Среда сама не планирует: она поставляет инструменты, `done_when` и безопасные точки. Правила (статусы,
выбор, срок) в `domain/tasks.py`; хранилище это `TaskRepository` (блокирующее, зовётся через `asyncio.to_thread`); события `task.*`
идут в журнал через `log(kind, text, **data)` (`events_log.log_nowait`). Напоминания себе и планы пользователя тоже задачи
(`kind`), у них есть срок: `ImpulseTimer` и `PlanNudger` берут сроки отсюда.
"""
import asyncio
import logging
import secrets
import time
from dataclasses import replace
from typing import Any, Callable

from contracts import Planner, TaskRepository
from domain import tasks as dom

logger = logging.getLogger("tasks")

ASSISTANT = "assistant"


class TaskError(Exception):
    """Понятная причина отказа для инструмента."""


class TaskService:
    def __init__(self, repo: TaskRepository, *, planner: Planner | None = None,
                 workspace_of: Callable[[], str] = lambda: ASSISTANT, context_of: Callable[[str], dict] = lambda ws: {"workspace": ws},
                 clock: Callable[[], float] = time.time, log: Callable[..., None] | None = None):
        """workspace_of() - текущая среда (`WorkspaceManager.current`); context_of(среда) - подсказка Planner (назначение,
        инструменты); log(kind, text, **данные) - журнал (`events_log.log_nowait`)."""
        self.repo, self.planner, self._workspace_of, self._context_of = repo, planner, workspace_of, context_of
        self.clock, self._log = clock, log
        self.reminders: Any = None                       # ReminderRepository поверх тех же задач (adapters/task_reminders.py)

    # ── служебное ────────────────────────────────────────────────────────────

    def _emit(self, name: str, t: dom.Task, **data) -> None:
        if self._log is not None:
            try:
                self._log(f"task.{name}", t.goal[:200], id=t.id, workspace=t.workspace, task_kind=t.kind, status=t.status, **data)
            except Exception as e:
                logger.warning(f"Журнал задач: {e}")

    def _emit_ws(self, name: str, workspace: str, **data) -> None:
        if self._log is not None:
            self._log(f"task.{name}", workspace, workspace=workspace, **data)

    async def _save(self, t: dom.Task) -> None:
        await asyncio.to_thread(self.repo.save, t.to_dict())

    async def _all(self, **kw) -> list[dom.Task]:
        return [dom.Task.from_dict(d) for d in await asyncio.to_thread(lambda: self.repo.list(**kw))]

    async def get(self, task_id: str) -> dom.Task | None:
        d = await asyncio.to_thread(self.repo.get, task_id)
        return dom.Task.from_dict(d) if d else None

    async def _need(self, task_id: str) -> dom.Task:
        t = await self.get(str(task_id))
        if t is None:
            raise TaskError(f"нет задачи {task_id}")
        return t

    def current_workspace(self) -> str:
        return self._workspace_of() or ASSISTANT

    # ── создание и разбиение ─────────────────────────────────────────────────

    async def create(self, goal: str, *, workspace: str | None = None, origin: str = dom.USER, parent: str | None = None,
                     priority: int = dom.PRIORITY_DEFAULT, due: float | None = None, steps: Any = (), plan: bool = False,
                     kind: str = dom.KIND_TASK, meta: dict | None = None, task_id: str | None = None) -> dom.Task:
        """Новая задача в среде (по умолчанию текущей). plan=True и шагов нет -> шаги подбирает Planner."""
        goal = str(goal or "").strip()
        if not goal:
            raise TaskError("у задачи должна быть цель")
        if parent is not None:
            await self._need(parent)
        t = dom.new_task(task_id or "t-" + secrets.token_hex(4), workspace or self.current_workspace(), goal, self.clock(),
                         origin=origin, parent=parent, priority=priority, due=due, steps=steps, kind=kind, meta=meta)
        if plan and not t.steps:
            t = await self._with_plan(t)
        await self._save(t)
        self._emit("created", t, origin=t.origin, due=t.due, steps=len(t.steps), parent=t.parent)
        return t

    async def _with_plan(self, t: dom.Task) -> dom.Task:
        if self.planner is None:
            return t
        try:
            raw = await self.planner.plan(t.goal, self._context_of(t.workspace))
        except Exception as e:
            logger.warning(f"Planner: {e}")
            raw = []
        return replace(t, steps=dom.make_steps(raw), updated=self.clock()) if raw else t

    async def plan(self, task_id: str) -> dom.Task:
        """Подобрать шаги задаче через Planner (прежние шаги заменяются)."""
        t = await self._need(task_id)
        if not t.is_open:
            raise TaskError("задача уже закрыта")
        if self.planner is None:
            raise TaskError("планировщик не подключён")
        planned = await self._with_plan(replace(t, steps=()))
        if not planned.steps:
            raise TaskError("не получилось разбить цель на шаги")
        await self._save(planned)
        self._emit("planned", planned, steps=len(planned.steps))
        return planned

    async def split(self, task_id: str) -> list[dom.Task]:
        """Разбить цель на подзадачи: каждый шаг плана становится дочерней задачей (среда, происхождение, срок наследуются)."""
        t = await self._need(task_id)
        if not t.steps:
            t = await self.plan(task_id)
        kids = [await self.create(s.text, workspace=t.workspace, origin=t.origin, parent=t.id, priority=t.priority, due=t.due)
                for s in t.steps if s.status == dom.STEP_PENDING]
        if kids:
            await self._refresh_parents(kids[0].id)
        return kids

    # ── статусы ──────────────────────────────────────────────────────────────

    async def set_status(self, task_id: str, status: str, *, note: str = "") -> dom.Task:
        t = await self._need(task_id)
        if status not in dom.STATUSES:
            raise TaskError(f"нет такого статуса: {status}")
        if t.status == status:
            return t
        if not dom.can_transition(t.status, status):
            raise TaskError(f"задача уже закрыта ({t.status})")
        if status == dom.ACTIVE:                              # в среде активна одна задача: прежняя ждёт в очереди
            for other in await self._all(workspace=t.workspace, statuses=(dom.ACTIVE,)):
                if other.id != t.id and other.kind != dom.KIND_REMINDER:
                    await self._save(dom.with_status(other, dom.PENDING, self.clock()))
        t = dom.with_status(t, status, self.clock())
        await self._save(t)
        self._emit("status", t, note=note[:200])
        await self._refresh_parents(t.id)
        return t

    async def activate(self, task_id: str) -> dom.Task:
        return await self.set_status(task_id, dom.ACTIVE)

    async def close(self, task_id: str, *, status: str = dom.DONE, note: str = "") -> dom.Task:
        """Закрыть задачу (done, failed, cancelled); подзадачи отменяются вместе с отменённой."""
        if status not in dom.TERMINAL:
            raise TaskError("закрыть можно как done, failed или cancelled")
        t = await self.set_status(task_id, status, note=note)
        if status == dom.CANCELLED:
            for kid in dom.descendants(await self._all(workspace=t.workspace), t.id):
                if kid.is_open:
                    await self._save(dom.with_status(kid, dom.CANCELLED, self.clock()))
        return t

    async def finish_step(self, task_id: str, index: int | None = None) -> dom.Task:
        t = await self._need(task_id)
        done = dom.finish_step(t, index, self.clock())
        if done is t:
            return t
        await self._save(done)
        self._emit("step", done, progress=list(dom.progress(done)))
        if done.status != t.status:
            self._emit("status", done)
            await self._refresh_parents(done.id)
        return done

    async def _refresh_parents(self, task_id: str) -> None:
        """Пересчитать статус родителей вверх по цепочке (родитель берёт состояние подзадач)."""
        seen: set[str] = set()
        cur = await self.get(task_id)
        while cur is not None and cur.parent and cur.parent not in seen:
            seen.add(cur.parent)
            parent = await self.get(cur.parent)
            if parent is None:
                return
            updated = dom.recompute_parent(parent, await self._all(parent=parent.id), self.clock())
            if updated is not parent:
                await self._save(updated)
                self._emit("status", updated, via="children")
            cur = updated

    # ── чтение и выбор ───────────────────────────────────────────────────────

    async def list_tasks(self, workspace: str | None = None, *, only_open: bool = True,
                   kinds: tuple[str, ...] | None = None) -> list[dom.Task]:
        """Задачи среды (по умолчанию текущей); `workspace="*"` - всех сред."""
        ws = self.current_workspace() if workspace is None else workspace
        found = await self._all(workspace=None if ws == "*" else ws, statuses=dom.OPEN if only_open else None)
        return [t for t in found if kinds is None or t.kind in kinds]

    async def active(self, workspace: str | None = None) -> dom.Task | None:
        """Активная задача среды: идущая или та, что выберет домен (приоритет, срок)."""
        ws = workspace or self.current_workspace()
        return dom.pick_active(await self._all(workspace=ws, statuses=(dom.ACTIVE, dom.PENDING)), ws)

    # ── checkpoint среды ─────────────────────────────────────────────────────

    async def checkpoint(self, workspace: str, goal: str = "") -> dict:
        """При уходе из среды: дерево её открытых задач, активная становится ожидающей, снимок пишется в задачи."""
        found = await self._all(workspace=workspace, statuses=dom.OPEN)
        snap = dom.checkpoint_of(found, workspace, goal, self.clock())
        for t in found:
            if t.kind != dom.KIND_REMINDER:
                await self._save(replace(t, checkpoint=snap, updated=self.clock(),
                                         status=dom.PENDING if t.status == dom.ACTIVE else t.status))
        if snap["tasks"]:
            self._emit_ws("checkpoint", workspace, count=len(snap["tasks"]))
        return snap

    async def resume(self, workspace: str) -> dom.Task | None:
        """При входе в среду: вернуть задачу из checkpoint (идущую раньше, иначе по приоритету) и сделать её активной."""
        t = await self.active(workspace)
        if t is None:
            return None
        if t.checkpoint:
            t = replace(t, checkpoint=None)
            await self._save(t)
        if t.status != dom.ACTIVE:
            t = await self.activate(t.id)
        self._emit("resumed", t)
        return t

    def on_workspace_change(self, rec: dict) -> None:
        """Хук `WorkspaceManager.on_change`: checkpoint уходящей среды, возобновление входящей (в фоне, цикл не ждёт диск)."""
        async def _run() -> None:
            await self.checkpoint(rec["from"], rec.get("goal", ""))
            await self.resume(rec["to"])
        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            return
        task.add_done_callback(lambda f: f.cancelled() or f.exception() is None or logger.warning(f"Задачи при смене среды: {f.exception()}"))

    # ── сроки и планы ────────────────────────────────────────────────────────

    async def due_for_nudge(self, lead_s: float = 0.0, kinds: tuple[str, ...] = (dom.KIND_PLAN,)) -> list[dom.Task]:
        """Задачи указанных видов, о которых пора напомнить (срок ближе `lead_s` или только что прошёл)."""
        found = await self._all(statuses=(dom.PENDING, dom.WAITING))
        return dom.due_for_nudge([t for t in found if t.kind in kinds], self.clock(), lead_s)

    async def upsert_plan(self, plan_id: str, text: str, due: float | None) -> dom.Task:
        """План пользователя как задача (зеркало записи памяти по `plan_id`); срок и текст обновляются."""
        for t in await self._all(kind=dom.KIND_PLAN):
            if t.meta.get("plan_id") == plan_id:
                if t.is_open and (t.goal != text or t.due != due):
                    t = replace(t, goal=str(text)[:dom.GOAL_MAX], due=due, updated=self.clock())
                    await self._save(t)
                return t
        return await self.create(text, workspace=ASSISTANT, origin=dom.USER, due=due, kind=dom.KIND_PLAN, meta={"plan_id": plan_id})

    async def snapshot(self, workspace: str | None = None) -> dict:
        """Для дашборда и инструмента: активная задача среды, открытые задачи с прогрессом."""
        ws = workspace or self.current_workspace()
        found = await self.list_tasks(ws, kinds=(dom.KIND_TASK, dom.KIND_PLAN))
        act = dom.pick_active(found, ws)
        return {"workspace": ws, "active": act.to_dict() if act else None,
                "tasks": [{**t.to_dict(), "progress": list(dom.progress(t))} for t in found]}
