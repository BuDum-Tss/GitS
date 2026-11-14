"""Менеджер сред (ТЗ 32, WS-2): в какой среде агент находится сейчас, почему и что было до этого.

Фокус это имя текущей среды плюс причина, цель и время входа. Правила входа и выхода живут в `services/spaces/model.py`; сам обмен с
модулем (освобождение, активация, checkpoint) делает менеджер фокуса ТЗ 18, его вызовы передаются сюда функциями
`activate(module, goal)` и `release(module, reason, force)`, поэтому менеджер проверяется тестами без модулей.

Среды модулей объявляет манифест (поле `workspace`, `register_manifest`). События чужих сред среду не переключают: они идут
в обычную очередь речи и сводку. Конец задачи, блокировка и отключение модуля возвращают в `assistant` (`check`).

Фокус один (`current`), а закреплённых за агентом окружений модулей может быть несколько (`held`): `enter` берёт окружение и
переводит фокус, прежнее окружение не отпускается и уходит в AFK; `afk` снимает фокус без отпускания аренды; `exit` отпускает
(отпускает только он, а также потеря связи с модулем). Правила в `docs/WORKSPACE_CONTRACT.md`, раздел 7.

Среда модуля включается только по импульсу (`user`, `self`) или по критическому сигналу; запуск игры, окно и состояние модуля её не
включают. `sleep` включает планировщик сна (`on_sleep_state`): если агент в среде модуля, сон идёт фоном и среду не меняет
(открытый вопрос 2 ТЗ 32). Команды пользователя «ляг спать» пока нет.
"""
import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

from services.spaces import model as dom

logger = logging.getLogger("workspace")

HISTORY_MAX = 300
GRACE_S = 5.0                  # после входа статусу модуля не верим: он может отстать (как в менеджере фокуса)

Activate = Callable[[str, str], Awaitable[dict]]
Release = Callable[[str, str, bool], Awaitable[dict]]
Afk = Callable[[str], Awaitable[dict]]


class WorkspaceManager:
    def __init__(self, *, activate: Activate | None = None, release: Release | None = None,
                 afk: Afk | None = None,
                 interruptible: Callable[[str], bool] = lambda ws: True,
                 connected: Callable[[str], bool] = lambda ws: True,
                 modules_of: Callable[[], Any] = lambda: (),
                 status_of: Callable[[str], dict | None] = lambda ws: None,
                 leased: Callable[[str], bool] = lambda ws: False,
                 lease_view: Callable[[str], dict | None] = lambda ws: None,
                 now: Callable[[], float] = time.time,
                 on_change: Callable[[dict], None] | None = None,
                 on_dropped: Callable[[str], None] | None = None,
                 log: Callable[..., None] | None = None):
        """activate/release/afk - обмен с модулем среды (afk(module) шлёт `workspace.afk`, аренду не трогает); interruptible(ws) - безопасная ли точка у среды модуля;
        modules_of() - (id, манифест, название) модулей, чьи среды объявлены; connected(ws) - подключён ли модуль; status_of(ws) - status из state модуля (blocked, done);
        on_change(запись) - после смены среды; on_dropped(ws) - закреплённое окружение в AFK потеряно (модуль отключился); log(тип, **поля) - журнал."""
        self.specs: dict[str, dom.WorkspaceSpec] = dict(dom.BUILTIN)
        self._activate, self._release, self._afk = activate, release, afk
        self.held: dict[str, dict] = {}      # закреплённые за агентом окружения модулей (фокус и AFK): id -> {goal, since, afk}
        self.entry: dict[str, dict] = {}     # вводная модуля из ответа на workspace.enter: id -> {brief, skills, units, site}
        self.on_dropped = on_dropped
        self._interruptible, self._connected = interruptible, connected
        self._status_of = status_of
        self._leased, self._lease_view = leased, lease_view
        self.refresh_leases: Callable[[], Awaitable] | None = None      # обновить сведения об арендуемых пространствах (ставит ядро)
        self._modules_of = modules_of
        self._now, self.on_change, self._log = now, on_change, log
        self.current = dom.ASSISTANT
        self.reason = dom.Reason.START
        self.goal = ""
        self.since = now()
        self.history: deque[dict] = deque(maxlen=HISTORY_MAX)
        self._lock = None            # создаётся лениво в цикле событий

    # ── среды ─────────────────────────────────────────────────────────────────

    def register_module(self, peer_id: str, title: str = "", purpose: str = "") -> dom.WorkspaceSpec:
        spec = dom.module_spec(peer_id, title, purpose)
        self.specs[peer_id] = spec
        return spec

    def register_manifest(self, peer_id: str, manifest: dict | None, title: str = "") -> dom.WorkspaceSpec | None:
        """Среда модуля по его манифесту (ТЗ 32, WS-4). Модуль без поля `workspace` среды не имеет: если она была, снимается
        (кроме текущей). Возвращает спецификацию или None."""
        ws = (manifest or {}).get("workspace")
        if not isinstance(ws, dict):
            if peer_id in self.specs and self.specs[peer_id].kind == dom.KIND_MODULE and peer_id != self.current:
                del self.specs[peer_id]
            return None
        spec = dom.spec_from_manifest(peer_id, ws, title)
        self.specs[peer_id] = spec
        return spec

    def refresh_modules(self, items) -> None:
        """Манифесты модулей изменились: items это (id, манифест, название)."""
        for peer_id, manifest, title in items:
            self.register_manifest(peer_id, manifest, title)

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
        already = False
        entry = None
        if spec.kind == dom.KIND_MODULE:
            if not self._connected(ws) and not self._leased(ws):     # общее пространство подключится при аренде
                return {"ok": False, "error": f"модуль «{spec.title}» не подключён"}
            if self._activate is not None:
                res = await self._activate(ws, goal)
                if not res.get("ok", False):
                    out = {"ok": False, "error": str(res.get("reason") or res.get("error") or "модуль не принял среду")}
                    if res.get("busy"):
                        out.update(busy=True, holder=res.get("holder"))
                    return out
                already = ws in self.held and bool(res.get("already", True))
                entry = res.get("entry") if isinstance(res.get("entry"), dict) and res.get("entry") else None
                if entry:
                    self.entry[ws] = entry
                else:
                    self.entry.pop(ws, None)
            held = self.held.get(ws) or {"since": self._now()}
            self.held[ws] = {**held, "goal": goal or held.get("goal", ""), "afk": False}
        if cur.kind == dom.KIND_MODULE and self.current in self.held:
            await self._to_afk(self.current)                           # прежнее окружение не отпускается: уходит в AFK
        self._set(ws, reason, goal)
        return {"ok": True, "workspace": ws, **({"already": True} if already else {}), **({"entry": entry} if entry else {})}

    async def _to_afk(self, ws: str) -> None:
        """Окружение остаётся за агентом, фокус с него снят: модулю идёт `workspace.afk`."""
        self.held[ws]["afk"] = True
        if self._afk is not None:
            try:
                await self._afk(ws)
            except Exception as e:
                logger.warning(f"AFK «{ws}»: {e}")

    async def afk(self, ws: str | None = None, reason: str = dom.Reason.USER) -> dict:
        """Снять фокус с окружения, не отпуская аренду (долгая операция идёт, вернуться можно через `enter`).
        Без аргумента - текущее. Окружение, уже стоящее в AFK, возвращается как `already`."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            ws = ws or self.current
            if ws not in self.held:
                return {"ok": False, "error": "это окружение не закреплено за ней" if ws in self.specs else f"нет такой среды: {ws}"}
            if ws != self.current:
                return {"ok": True, "already": True, "workspace": ws}
            ok, why = dom.can_exit(self.specs[ws], self._interruptible(ws), reason)
            if not ok:
                return {"ok": False, "error": why}
            await self._to_afk(ws)
            self._set(dom.exit_target(reason), reason, "", note="afk")
            return {"ok": True, "workspace": ws}

    async def exit(self, ws: str | None = None, reason: str = dom.Reason.USER) -> dict:
        """Выйти из окружения и отпустить аренду (без аргумента - текущее). Идущие операции отпускание ждёт у самого модуля:
        аренда остаётся в состоянии `releasing` (`releasing: True` в ответе). Отказ модуля без force оставляет всё как есть."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await self._exit(ws or self.current, reason)

    async def _exit(self, ws: str, reason: str) -> dict:
        if ws not in self.held:
            return {"ok": False, "error": "это окружение не закреплено за ней" if ws in self.specs else f"нет такой среды: {ws}"}
        is_focus = ws == self.current
        if is_focus:
            ok, why = dom.can_exit(self.specs[ws], self._interruptible(ws), reason)
            if not ok:
                return {"ok": False, "error": why}
        if self._release is not None:
            res = await self._release(ws, reason, reason in dom.Reason.FORCED)
            if not res.get("ok", False) and reason not in dom.Reason.FORCED:
                return {"ok": False, "error": str(res.get("reason") or res.get("error") or "модуль не отдал среду")}
        del self.held[ws]
        self.entry.pop(ws, None)
        view =self._lease_view(ws) or {}
        if view.get("releasing") and self.refresh_leases is not None:
            try:
                await self.refresh_leases()                            # сколько операций ещё идёт, знает только сервис
                view = self._lease_view(ws) or view
            except Exception as e:
                logger.warning(f"Состояние аренды «{ws}»: {e}")
        out = {"ok": True, "workspace": ws, "releasing": bool(view.get("releasing")), "jobs": int(view.get("jobs") or 0)}
        if is_focus:
            self._set(dom.exit_target(reason), reason, "", note="exit")
        elif self._log is not None:
            self._log("exit", **{"from": ws, "to": self.current, "reason": reason, "goal": "", "note": "exit"})
        return out

    async def leave(self, reason: str = dom.Reason.DONE, goal: str = "") -> dict:
        """Выйти из текущей среды в базовую с отпусканием (кнопка «выйти», просьба пользователя). Из базовой среды выходить некуда."""
        if self.current in self.held:
            return await self.exit(self.current, reason)
        return await self.enter(dom.exit_target(reason), reason, goal)

    # ── сон: решает планировщик сна (ТЗ 27) ───────────────────────────────────

    def on_sleep_state(self, state: str) -> None:
        if state == "sleeping" and self.current == dom.ASSISTANT:
            self._set(dom.SLEEP, dom.Reason.SCHEDULE, "")
        elif state != "sleeping" and self.current == dom.SLEEP:
            self._set(dom.ASSISTANT, dom.Reason.DONE, "")

    def on_module_gone(self, peer_id: str) -> None:
        """Модуль отключился: если агент был в его среде, возврат в базовую (checkpoint остаётся у менеджера фокуса)."""
        was = self.held.pop(peer_id, None)
        self.entry.pop(peer_id, None)
        if self.current == peer_id:
            self._set(dom.exit_target(dom.Reason.DISCONNECT), dom.Reason.DISCONNECT, "", note=peer_id)
        elif was is not None and self.on_dropped is not None:
            try:
                self.on_dropped(peer_id)                              # окружение в AFK потеряно вместе с модулем
            except Exception as e:
                logger.warning(f"Потеря окружения «{peer_id}»: {e}")

    def check(self) -> None:
        """Состояние модулей изменилось: модуль текущей среды отключился, сообщил блокировку или конец задачи (`done`) - вернуться в базовую среду. Checkpoint при этом ведёт менеджер фокуса."""
        for ws in [w for w in self.held if w != self.current and not self._connected(w)]:
            self.on_module_gone(ws)                                    # закреплённое окружение в AFK: модуль ушёл
        if self.spec().kind != dom.KIND_MODULE:
            return
        if not self._connected(self.current):
            self.on_module_gone(self.current)
            return
        if self._now() - self.since < GRACE_S:
            return
        st = self._status_of(self.current) or {}
        if st.get("blocked"):
            self.held.pop(self.current, None)
            self._set(dom.exit_target(dom.Reason.BLOCKED), dom.Reason.BLOCKED, "", note=str(st["blocked"])[:160])
        elif st.get("done"):
            self.held.pop(self.current, None)
            self._set(dom.exit_target(dom.Reason.DONE), dom.Reason.DONE, "", note=self.current)

    async def run(self, interval: float = 2.0) -> None:
        """Периодическая проверка: статус модуля мог измениться без события."""
        while True:
            try:
                self.check()
            except Exception as e:
                logger.warning(f"Проверка среды: {e}")
            await asyncio.sleep(interval)

    # ── чтение ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        self.sync()
        spec = self.spec()
        return {
            "current": self.current, "title": spec.title, "kind": spec.kind, "purpose": spec.purpose,
            "reason": self.reason, "goal": self.goal, "since": self.since,
            "held": [{"id": ws, "title": self.specs[ws].title if ws in self.specs else ws, "goal": h.get("goal", ""),
                      "since": h.get("since"), "afk": ws != self.current} for ws, h in self.held.items()],
            "available": [{"id": s.id, "title": s.title, "kind": s.kind, "entry": list(s.entry),
                           "purpose": s.purpose, "when_to_use": s.when_to_use, "lease": self._lease_view(s.id),
                           "connected": s.kind == dom.KIND_BUILTIN or bool(self._connected(s.id))}
                          for s in self.specs.values()],
            "history": list(self.history)[-20:],
        }
