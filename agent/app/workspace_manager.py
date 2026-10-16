"""Менеджер сред (ТЗ 32, WS-2): в какой среде агент находится сейчас, почему и что было до этого.

Фокус это имя текущей среды плюс причина, цель и время входа. Правила входа и выхода живут в `domain/workspace.py`; сам обмен с
модулем (освобождение, активация, checkpoint) делает менеджер фокуса ТЗ 18, его вызовы передаются сюда функциями
`activate(module, goal)` и `release(module, reason, force)`, поэтому менеджер проверяется тестами без модулей.

Среды модулей объявляет манифест (поле `workspace`, `register_manifest`). События чужих сред не борются за слот: в текущей среде
это сигналы (`on_event`, `domain.workspace.route_signal`); критический сигнал (срочность 5) входит в среду его модуля по
правилам без LLM, и текущая среда вправе отказать в небезопасной точке. Конец задачи, блокировка и отключение модуля возвращают
в `assistant` (`check`).

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
GRACE_S = 5.0                  # после входа статусу модуля не верим: он может отстать (как в менеджере фокуса)
CRITICAL_BACKOFF_S = 30.0      # критический вход не удался: повтор к тому же модулю не раньше чем через столько
SIGNALS_KEEP = 20

Activate = Callable[[str, str], Awaitable[dict]]
Release = Callable[[str, str, bool], Awaitable[dict]]


class WorkspaceManager:
    def __init__(self, *, activate: Activate | None = None, release: Release | None = None,
                 interruptible: Callable[[str], bool] = lambda ws: True,
                 connected: Callable[[str], bool] = lambda ws: True,
                 modules_of: Callable[[], Any] = lambda: (),
                 status_of: Callable[[str], dict | None] = lambda ws: None,
                 is_background: Callable[[str, str | None], bool] = lambda ws, policy: False,
                 now: Callable[[], float] = time.time,
                 on_change: Callable[[dict], None] | None = None,
                 log: Callable[..., None] | None = None):
        """activate/release - обмен с модулем среды; interruptible(ws) - безопасная ли точка у среды модуля;
        modules_of() - (id, манифест, название) модулей, чьи среды объявлены; connected(ws) - подключён ли модуль; status_of(ws) - status из state модуля (active_policy, blocked);
        is_background(ws, policy) - фоновая ли политика (возврат в неё значит «задача кончилась»); on_change(запись) - после смены среды; log(тип, **поля) - журнал."""
        self.specs: dict[str, dom.WorkspaceSpec] = dict(dom.BUILTIN)
        self._activate, self._release = activate, release
        self._interruptible, self._connected = interruptible, connected
        self._status_of, self._is_background = status_of, is_background
        self._modules_of = modules_of
        self._now, self.on_change, self._log = now, on_change, log
        self.current = dom.ASSISTANT
        self.reason = dom.Reason.START
        self.goal = ""
        self.since = now()
        self.history: deque[dict] = deque(maxlen=HISTORY_MAX)
        self._lock = None            # создаётся лениво в цикле событий
        self.signals = {"notify": 0, "digest": 0, "switch": 0, "refused": 0}
        self.recent_signals: deque[dict] = deque(maxlen=SIGNALS_KEEP)
        self._critical_block: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ── среды ─────────────────────────────────────────────────────────────────

    def register_module(self, module_id: str, title: str = "", purpose: str = "") -> dom.WorkspaceSpec:
        spec = dom.module_spec(module_id, title, purpose)
        self.specs[module_id] = spec
        return spec

    def register_manifest(self, module_id: str, manifest: dict | None, title: str = "") -> dom.WorkspaceSpec | None:
        """Среда модуля по его манифесту (ТЗ 32, WS-4). Модуль без поля `workspace` среды не имеет: если она была, снимается
        (кроме текущей). Возвращает спецификацию или None."""
        ws = (manifest or {}).get("workspace")
        if not isinstance(ws, dict):
            if module_id in self.specs and self.specs[module_id].kind == dom.KIND_MODULE and module_id != self.current:
                del self.specs[module_id]
            return None
        spec = dom.spec_from_manifest(module_id, ws, title)
        self.specs[module_id] = spec
        return spec

    def refresh_modules(self, items) -> None:
        """Манифесты модулей изменились: items это (id, манифест, название)."""
        for module_id, manifest, title in items:
            self.register_manifest(module_id, manifest, title)

    def sync(self) -> None:
        """Подхватить среды из манифестов подключённых модулей (читается при каждом обращении, модули приходят и уходят)."""
        try:
            self.refresh_modules(self._modules_of())
        except Exception as e:
            logger.warning(f"Среды модулей: {e}")

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
        self.sync()
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
        """Состояние модулей изменилось: модуль текущей среды отключился, сообщил блокировку или вернулся в фоновую политику
        (задача кончилась) - вернуться в базовую среду. Checkpoint при этом ведёт менеджер фокуса."""
        if self.spec().kind != dom.KIND_MODULE:
            return
        if not self._connected(self.current):
            self.on_module_gone(self.current)
            return
        if self._now() - self.since < GRACE_S:
            return
        st = self._status_of(self.current) or {}
        if st.get("blocked"):
            self._set(dom.exit_target(dom.Reason.BLOCKED), dom.Reason.BLOCKED, "", note=str(st["blocked"])[:160])
        elif st.get("active_policy") is not None and self._is_background(self.current, st["active_policy"]):
            self._set(dom.exit_target(dom.Reason.DONE), dom.Reason.DONE, "", note=self.current)

    async def run(self, interval: float = 2.0) -> None:
        """Периодическая проверка: статус модуля мог измениться без события."""
        while True:
            try:
                self.check()
            except Exception as e:
                logger.warning(f"Проверка среды: {e}")
            await asyncio.sleep(interval)

    # ── сигналы: события чужих сред (WS-6) ────────────────────────────────────

    def on_event(self, item: Any, decision: str = "", priority: float = 0.0, rule: str | None = None) -> None:
        """Хук `Attention.on_decided`: событие модуля. Синхронный и быстрый; вход по критическому сигналу идёт фоновой задачей."""
        if decision == "D0" or rule in ("R1", "R2", "R3"):          # выброшено или разговор с пользователем / mute
            return
        module = getattr(item, "module", None)
        if not module:
            return
        route = self.route(module, getattr(item, "kind", "") or "", getattr(item, "urgency", None))
        self._count(route, module, getattr(item, "kind", "") or "")
        if route == dom.SIGNAL_SWITCH:
            try:
                task = asyncio.get_running_loop().create_task(
                    self._switch_critical(module, (getattr(item, "text", "") or "")[:200]))
            except RuntimeError:
                return
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def route(self, module: str, kind: str, urgency: int | None) -> str:
        self.sync()
        return dom.route_signal(self.spec(), self.specs.get(module), kind, module, urgency)

    def _count(self, route: str, module: str, kind: str) -> None:
        if route in self.signals:
            self.signals[route] += 1
        if route != dom.SIGNAL_OWN:
            self.recent_signals.append({"ts": self._now(), "module": module, "kind": kind, "route": route})

    async def handle_signal(self, module: str, kind: str, text: str = "", urgency: int | None = None) -> dict:
        """То же, что on_event, но с ожиданием результата (тесты, прямые вызовы): {"route", "entered", "error"}."""
        route = self.route(module, kind, urgency)
        self._count(route, module, kind)
        if route != dom.SIGNAL_SWITCH:
            return {"route": route, "entered": False}
        res = await self._switch_critical(module, text[:200])
        return {"route": route, "entered": bool(res.get("ok")), "error": res.get("error", "")}

    async def _switch_critical(self, module: str, goal: str) -> dict:
        now = self._now()
        if now < self._critical_block.get(module, 0.0):
            return {"ok": False, "error": "недавний критический вход не удался: повтор позже"}
        res = await self.enter(module, dom.Reason.CRITICAL, goal)
        if not res.get("ok"):
            self.signals["refused"] += 1
            self._critical_block[module] = now + CRITICAL_BACKOFF_S
            if self._log is not None:
                self._log("critical_refused", to=module, reason=dom.Reason.CRITICAL, note=str(res.get("error", ""))[:160])
        return res

    # ── чтение ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        self.sync()
        spec = self.spec()
        return {
            "current": self.current, "title": spec.title, "kind": spec.kind, "purpose": spec.purpose,
            "reason": self.reason, "goal": self.goal, "since": self.since,
            "available": [{"id": s.id, "title": s.title, "kind": s.kind, "entry": list(s.entry),
                           "connected": s.kind == dom.KIND_BUILTIN or bool(self._connected(s.id))}
                          for s in self.specs.values()],
            "history": list(self.history)[-20:],
            "signals": {**self.signals, "recent": list(self.recent_signals)},
        }
