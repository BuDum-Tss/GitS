"""ReminderRepository поверх общих задач (ТЗ 32, WS-8): напоминание себе это задача `kind=reminder` с `due`.

Тот же интерфейс, что у `JsonReminders` (ТЗ 30), поэтому `ImpulseTimer` и инструмент `assistant.remind_self` не меняются. Запись
напоминания: id, due, text, source, status (`pending` / `fired` / `cancelled`), depth, untrusted, created, ttl_s. Статус `fired`
хранится задачей как `done`. Методы блокирующие (ImpulseTimer зовёт их через `asyncio.to_thread`).
"""
import secrets
import time
from typing import Callable

from contracts import TaskRepository
from domain import tasks as dom

_TO_TASK = {"pending": dom.PENDING, "fired": dom.DONE, "cancelled": dom.CANCELLED}
_FROM_TASK = {dom.PENDING: "pending", dom.DONE: "fired", dom.CANCELLED: "cancelled"}


def to_reminder(t: dict) -> dict:
    meta = t.get("meta") or {}
    return {"id": t["id"], "due": float(t["due"] or 0.0), "text": t["goal"], "source": meta.get("source") or {},
            "status": _FROM_TASK.get(t["status"], "pending"), "depth": int(meta.get("depth") or 0),
            "untrusted": bool(meta.get("untrusted")), "created": t.get("created") or 0.0, "ttl_s": meta.get("ttl_s")}


class TaskReminders:
    def __init__(self, repo: TaskRepository, *, workspace_of: Callable[[], str] = lambda: "assistant",
                 clock: Callable[[], float] = time.time, log: Callable[..., None] | None = None):
        self.repo, self._workspace_of, self._clock, self._log = repo, workspace_of, clock, log

    def _emit(self, name: str, task: dict, **data) -> None:
        if self._log is not None:
            self._log(f"task.{name}", task["goal"][:200], id=task["id"], workspace=task["workspace"], task_kind=dom.KIND_REMINDER, **data)

    def add(self, due: float, text: str, *, source: dict | None = None, depth: int = 0, untrusted: bool = False,
            ttl_s: float | None = None) -> dict:
        src = source or {}
        origin = dom.USER if src.get("kind") == "plan" else dom.SELF            # напоминание о плане пользователя идёт от его слов
        t = dom.new_task("r-" + secrets.token_hex(4), self._workspace_of(), str(text)[:500], self._clock(), origin=origin, due=float(due),
                         kind=dom.KIND_REMINDER, meta={"source": src, "depth": int(depth), "untrusted": bool(untrusted), "ttl_s": ttl_s})
        self.repo.save(t.to_dict())
        self._emit("created", t.to_dict(), due=float(due), origin=origin)
        return to_reminder(t.to_dict())

    def get(self, reminder_id: str) -> dict | None:
        t = self.repo.get(reminder_id)
        return to_reminder(t) if t is not None and t.get("kind") == dom.KIND_REMINDER else None

    def pending(self) -> list[dict]:
        rows = self.repo.list(kind=dom.KIND_REMINDER, statuses=(dom.PENDING,))
        return sorted((to_reminder(r) for r in rows if r.get("due") is not None), key=lambda r: r["due"])

    def set_status(self, reminder_id: str, status: str) -> None:
        t = self.repo.get(reminder_id)
        if t is None or t.get("kind") != dom.KIND_REMINDER:
            return
        new = _TO_TASK.get(status, dom.PENDING)
        if not dom.can_transition(t["status"], new) or t["status"] == new:
            return
        t["status"], t["updated"] = new, self._clock()
        self.repo.save(t)
        self._emit("status", t, status=new)
