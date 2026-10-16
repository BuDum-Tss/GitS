"""Менеджер сред (ТЗ 32, WS-2): в какой среде агент находится сейчас, почему и что было до этого.

Фокус это имя текущей среды плюс причина, цель и время входа. Правила входа и выхода живут в `domain/workspace.py`; сам обмен с
модулем (освобождение, активация, checkpoint) делает менеджер фокуса ТЗ 18, его вызовы передаются сюда функциями
`activate(module, goal)` и `release(module, reason, force)`, поэтому менеджер проверяется тестами без модулей.

Среда модуля включается только по импульсу (`user`, `self`) или по критическому сигналу; запуск игры, окно и состояние модуля её не
включают. `sleep` включает планировщик сна (`on_sleep_state`): если агент в среде модуля, сон идёт фоном и среду не меняет
(открытый вопрос 2 ТЗ 32). Команды пользователя «ляг спать» пока нет.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from domain import workspace as dom

logger = logging.getLogger("workspace")

HISTORY_MAX = 300

Activate = Callable[[str, str], Awaitable[dict]]
Release = Callable[[str, str, bool], Awaitable[dict]]


class WorkspaceManager:
    def __init__(self, *, activate: Activate | None = None, release: Release | None = None,
                 interruptible: Callable[[str], bool] = lambda ws: True,
                 connected: Callable[[str], bool] = lambda ws: True,
                 now: Callable[[], float] = time.time,
                 on_change: Callable[[dict], None] | None = None,
                 log: Callable[..., None] | None = None):
        """activate/release - обмен с модулем среды; interruptible(ws) - безопасная ли точка у среды модуля;
        connected(ws) - подключён ли модуль; on_change(запись) - после смены среды; log(тип, **поля) - журнал."""
        self.specs: dict[str, dom.WorkspaceSpec] = dict(dom.BUILTIN)
        self._activate, self._release = activate, release
        self._interruptible, self._connected = interruptible, connected
        self._now, self.on_change, self._log = now, on_change, log
        self.current = dom.ASSISTANT
        self.reason = dom.Reason.START
        self.goal = ""
        self.since = now()
        self.history: deque[dict] = deque(maxlen=HISTORY_MAX)
        self._lock = None            # создаётся лениво в цикле событий

    # ── среды ─────────────────────────────────────────────────────────────────

    def register_module(self, module_id: str, title: str = "", purpose: str = "") -> dom.WorkspaceSpec:
        spec = dom.module_spec(module_id, title, purpose)
        self.specs[module_id] = spec
        return spec

    def spec(self) -> dom.WorkspaceSpec:
        return self.specs[self.current]

    # ── смена ─────────────────────────────────────────────────────────────────

    def _set(self, ws: str, reason: str, goal: str, note: str = "") -> dict:
        now = self._now()
        rec = dom.switch_record(now, self.current, ws, reason, goal, note)
        self.current, self.reason, self.goal, self.since = ws, reason, goal, now
        self.history.append(rec)
        if self._log is not None:
            self._log("switch", **{k: v for k, v in rec.items() if k != "ts"})
        if self.on_change is not None:
            try:
                self.on_change(rec)
            except Exception as e:
                logger.warning(f"Реакция на смену среды: {e}")
        return rec

    async def enter(self, ws: str, reason: str, goal: str = "") -> dict:
        """Войти в среду. {"ok": True, "workspace": ...} или {"ok": False, "error": причина}; при отказе среда не меняется."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await self._enter(ws, reason, goal)

    async def _enter(self, ws: str, reason: str, goal: str) -> dict:
        spec = self.specs.get(ws)
        if spec is None:
            return {"ok": False, "error": f"нет такой среды: {ws}"}
        ok, why = dom.can_enter(spec, reason)
        if not ok:
            return {"ok": False, "error": why}
        if ws == self.current:
            self.goal = goal or self.goal
            return {"ok": True, "already": True, "workspace": ws}
        cur = self.specs[self.current]
        ok, why = dom.can_exit(cur, self._interruptible(self.current), reason)
        if not ok:
            return {"ok": False, "error": why}
        if spec.kind == dom.KIND_MODULE:
            if not self._connected(ws):
                return {"ok": False, "error": f"модуль «{spec.title}» не подключён"}
            if self._activate is not None:
                res = await self._activate(ws, goal)
                if not res.get("ok", False):
                    return {"ok": False, "error": str(res.get("reason") or res.get("error") or "модуль не принял среду")}
        elif cur.kind == dom.KIND_MODULE and self._release is not None:
            res = await self._release(self.current, reason, reason in dom.Reason.FORCED)
            if not res.get("ok", False) and reason not in dom.Reason.FORCED:
                return {"ok": False, "error": str(res.get("reason") or res.get("error") or "модуль не отдал среду")}
        self._set(ws, reason, goal)
        return {"ok": True, "workspace": ws}

    async def leave(self, reason: str = dom.Reason.DONE, goal: str = "") -> dict:
        """Выйти из текущей среды в базовую (конец задачи, блокировка, отключение, просьба пользователя)."""
        return await self.enter(dom.exit_target(reason), reason, goal)

    # ── сон: решает планировщик сна (ТЗ 27) ───────────────────────────────────

    def on_sleep_state(self, state: str) -> None:
        if state == "sleeping" and self.current == dom.ASSISTANT:
            self._set(dom.SLEEP, dom.Reason.SCHEDULE, "")
        elif state != "sleeping" and self.current == dom.SLEEP:
            self._set(dom.ASSISTANT, dom.Reason.DONE, "")

    def on_module_gone(self, module_id: str) -> None:
        """Модуль отключился: если агент был в его среде, возврат в базовую (checkpoint остаётся у менеджера фокуса)."""
        if self.current == module_id:
            self._set(dom.exit_target(dom.Reason.DISCONNECT), dom.Reason.DISCONNECT, "", note=module_id)

    def check(self) -> None:
        """Состояние модулей изменилось: если модуль текущей среды отключился, вернуться в базовую."""
        if self.spec().kind == dom.KIND_MODULE and not self._connected(self.current):
            self.on_module_gone(self.current)

    # ── чтение ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        spec = self.spec()
        return {
            "current": self.current, "title": spec.title, "kind": spec.kind, "purpose": spec.purpose,
            "reason": self.reason, "goal": self.goal, "since": self.since,
            "available": [{"id": s.id, "title": s.title, "kind": s.kind, "entry": list(s.entry),
                           "connected": s.kind == dom.KIND_BUILTIN or bool(self._connected(s.id))}
                          for s in self.specs.values()],
            "history": list(self.history)[-20:],
        }
