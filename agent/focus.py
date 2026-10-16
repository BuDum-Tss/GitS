"""Менеджер фокуса (ТЗ 18, FOC-1…FOC-9): единственный слот фокуса над модулями, очередь событий, протокол переключения,
checkpoint задач, дребезг, пользовательские «закрепить», «не беспокоить» и разовое исключение, LLM-диспетчер для
неоднозначных случаев, аудит.

Фасад над четырьмя частями (ТЗ 29, ARC-6): `domain/focus_policy.py` (FocusPolicy: чистые правила, пороги, дребезг,
эшелон), `focus_queue.py` (FocusQueue: состояния событий, склейка, старение, сроки), `focus_store.py` (CheckpointStore:
checkpoint и файл `focus.json`), `focus_dispatch.py` (FocusDispatch: контракт к модели). Здесь остаются слот, протокол
переключения, команды пользователя, журнал и снимок для дашборда; публичные методы прежние.

Надстройка над вниманием (attention.py): внимание оценивает событие и решает D0–D4, а менеджер фокуса получает решение
через хук `Attention.on_decided` и решает, переключать ли модуль в активную политику. Слот фокуса один: в каждый момент
модуль в политике `active` (или никто, тогда фокус у пользователя и разговора). Остальные модули в фоновых политиках.

Протокол переключения (раздел 7 исходного документа): evaluate → check interruption → checkpoint → release → activate →
validate → resume/schedule. Запросы к модулю: `focus.release` (модуль сохраняет checkpoint и отвечает его содержимым, пока
он не ответил, фокус не передан), `policy.activate`, `focus.resume` (для модулей без него - `policy.activate` с
checkpoint, см. docs/FOCUS.md).

Два эшелона решения (ТЗ 18, docs/FOCUS.md раздел 9): первый - GLiNER (decide.py) даёт подсказку политики, уверенность и
нужен ли фокус; если уверенность высокая и правила однозначны, решение принимается по правилам без LLM. Второй - Gemma
(focus_dispatch.py) вызывается только для неоднозначных случаев. Каждое решение помечено эшелоном (`rules`, `gliner`,
`gemma`) и уверенностью в журнале.

Состояние (очередь, checkpoint, пины, «не беспокоить») живёт в памяти и записывается в файл (`focus.json` в папке памяти),
чтобы перезапуск ядра не терял прерванные задачи. «Не беспокоить» для всего агента общее с настройкой ядра `dnd` /
`quiet_until` (одно состояние пользователя), разовые исключения и закрепление живут здесь. Все сетевые вызовы асинхронные,
тайм-ауты короткие; критические события не ждут LLM."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import events_log
import focus_dispatch as dispatch
from domain import focus_policy as policy
from domain.focus_policy import (ECHELON_GEMMA, ECHELON_GLINER, ECHELON_RULES, SWITCH_P, MARGIN, CONF_LOW,  # noqa: F401
                                 CONF_HIGH, CRITICAL_URGENCY, USER_PRIORITY, HOLDER_PRIORITY_EVENT, RECHECK_CONF,
                                 DANGER_CATEGORIES)
from focus_dispatch import LLM_TIMEOUT_S  # noqa: F401
from focus_queue import (PENDING, DEFERRED, PROCESSING, RESOLVED, EXPIRED, DISCARDED, EVENT_ALIVE,  # noqa: F401
                         RECHECK_S, EVENT_TTL_S, DEDUPE_S, RESOLVED_KEEP_S, FocusQueue, QueuedEvent, decision_fields,
                         event_echelon, new_id)
from focus_store import (CHECKPOINT_MAX_AGE_S, CHECKPOINT_MAX_BYTES, CHECKPOINTS_KEEP, RESUME_RETRIES,  # noqa: F401
                         STATE_FILE, CheckpointStore)
from module_hub import ModuleError

logger = logging.getLogger("focus")

# ── сроки протокола (пороги правил живут в domain/focus_policy.py) ───────────────────────────
GRACE_S = 5.0                  # после передачи фокуса статусу модуля не верим этот срок (он может отстать)
REFUSE_BACKOFF_S = 60.0         # модуль отказал в переходе: повтор к нему не раньше чем через столько (не трогая фокус)
RELEASE_TIMEOUT_S = 15.0
ACTIVATE_TIMEOUT_S = 20.0
RESUME_TIMEOUT_S = 20.0
JOURNAL_MAX = 300
TICK_S = 0.5
MAX_POLICY_OPTIONS = 12

_EVENT_ALIVE = EVENT_ALIVE
_confirmed = policy.confirmed                               # domain/focus_policy.py
Connect = Callable[[str], Any]


@dataclass
class Slot:
    """Слот фокуса: модуль, задача и политика, которая сейчас активна."""

    module: str
    task_id: str
    policy: str
    goal: str
    priority: float
    since: float
    origin: str                 # event | user | resume
    event_id: str | None = None
    echelon: str = ECHELON_RULES

    def public(self, pin: str | None) -> dict:
        return {"module": self.module, "task_id": self.task_id, "policy": self.policy, "goal": self.goal[:160],
                "priority": self.priority, "since": self.since, "origin": self.origin, "pinned": pin == self.module,
                "echelon": self.echelon}


class FocusManager:
    def __init__(self, *, connection_of: Connect, status_of: Callable[[str], dict | None],
                 policies_of: Callable[[str], dict | None], clock: Callable[[], float] = time.time,
                 state_path: Path | None = None, llm: Callable[[dict], Awaitable[Any]] | None = None,
                 busy_of: Callable[[], bool] | None = None,
                 recheck: Callable[[dict], Awaitable[Any]] | None = None, title_of: Callable[[str], str] | None = None,
                 dnd_all_of: Callable[[], bool] | None = None, set_dnd_all: Callable[[bool], None] | None = None,
                 profile_quiet_of: Callable[[], Any] | None = None, modules_of: Callable[[], Any] | None = None):
        """connection_of(модуль) -> подключение с request(type, body, timeout) или None; status_of(модуль) -> status из
        `state` (active_policy, interruptible, blocked, ...) или None; policies_of(модуль) -> {default_policy, policies}
        из манифеста или None (модуль без политик - без фокуса, режим совместимости); busy_of() -> True, пока пользователь
        в разговоре; llm(контекст) -> ответ диспетчера (None - LLM выключена); recheck(событие) -> новая срочность (0..5).
        dnd_all_of() / set_dnd_all(on) - общее состояние «не беспокоить» для всего агента (настройки ядра dnd/quiet_until);
        без них хранится здесь. profile_quiet_of() -> модули, которым профиль не даёт переключать фокус (не сохраняется);
        modules_of() -> ids модулей для подсказок политики (по умолчанию модули, которые присылали события)."""
        self.connection_of = connection_of
        self.status_of = status_of
        self.policies_of = policies_of
        self.clock = clock
        self.busy_of = busy_of
        self.title_of = title_of or (lambda mid: mid)
        self.slot: Slot | None = None
        self.pinned: str | None = None
        self._dnd_all_of = dnd_all_of
        self._set_dnd_all = set_dnd_all
        self._profile_quiet_of = profile_quiet_of
        self._modules_of = modules_of
        self._dnd_local = False
        self.dnd_modules: set[str] = set()
        self.store = CheckpointStore(state_path)
        self.queue = FocusQueue(log=self._log, touch=self.store.touch)
        self.dispatch = dispatch.FocusDispatch(llm, recheck)
        self._journal: deque = deque(maxlen=JOURNAL_MAX)
        self._switch_times: deque = deque(maxlen=64)
        self._refused_at: dict[str, float] = {}      # модуль -> когда он последний раз отказал в переходе
        self._sig: tuple | None = None
        self._last_llm: dict | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    # ── доступ к частям (прежние имена) ────────────────────────────────────

    @property
    def events(self) -> dict[str, QueuedEvent]:
        return self.queue.events

    @property
    def checkpoints(self) -> dict[str, dict]:
        return self.store.checkpoints

    @property
    def state_path(self) -> Path | None:
        return self.store.state_path

    @property
    def _once(self) -> set[str]:
        return self.queue.once

    @property
    def _modules_seen(self) -> set[str]:
        return self.queue.modules_seen

    @property
    def _inbox(self) -> list[dict]:
        return self.queue.inbox

    @property
    def _dirty(self) -> bool:
        return self.store.dirty

    @_dirty.setter
    def _dirty(self, value: bool) -> None:
        self.store.dirty = value

    @property
    def llm(self) -> Callable[[dict], Awaitable[Any]] | None:
        return self.dispatch.llm

    @llm.setter
    def llm(self, value: Callable[[dict], Awaitable[Any]] | None) -> None:
        self.dispatch.llm = value

    @property
    def recheck(self) -> Callable[[dict], Awaitable[Any]] | None:
        return self.dispatch.recheck

    @recheck.setter
    def recheck(self, value: Callable[[dict], Awaitable[Any]] | None) -> None:
        self.dispatch.recheck = value

    @property
    def dnd_all(self) -> bool:
        """«Не беспокоить» для всего агента: одно состояние с настройкой ядра (ТЗ 18, FOC-6)."""
        return bool(self._dnd_all_of()) if self._dnd_all_of is not None else self._dnd_local

    def _profile_quiet(self) -> set[str]:
        """Модули, которым активный профиль не даёт переключать фокус (`focus.quiet_modules`); не сохраняется."""
        if self._profile_quiet_of is None:
            return set()
        try:
            return {m for m in (self._profile_quiet_of() or ()) if isinstance(m, str)}
        except Exception:                                          # профиль не должен ронять менеджер фокуса
            logger.debug("профиль не отдал модули фокуса", exc_info=True)
            return set()

    def _quiet_modules(self) -> set[str]:
        return set(self.dnd_modules) | self._profile_quiet()

    def _known_modules(self) -> list[str]:
        if self._modules_of is not None:
            return sorted(str(m) for m in self._modules_of())
        return sorted(self._modules_seen)

    def policy_options(self) -> dict[str, str]:
        """Варианты подсказки политики для GLiNER (ТЗ 18, первый эшелон): активные политики подключённых модулей
        с назначением из манифеста. Ключ «модуль/политика»."""
        out: dict[str, str] = {}
        for mod in self._known_modules():
            if self.connection_of(mod) is None:
                continue
            for pid, pol in (self._policy_table(mod) or {}).items():
                if pol.get("focus") == "active":
                    out[f"{mod}/{pid}"] = str(pol.get("purpose") or pol.get("title") or pid)[:160]
        return dict(list(out.items())[:MAX_POLICY_OPTIONS])

    # ── вход: решение внимания ──────────────────────────────────────────────

    def on_decided(self, item: Any, decision: str, priority: float, rule: str | None = None) -> None:
        """Хук `Attention.on_decided`: событие с решением внимания. Синхронный и быстрый: только кладёт запись в очередь
        входа, сама очередь меняется в tick(). Не переключает фокус сразу, чтобы не звать модуль из обработчика внимания."""
        if decision == "D0" or rule in ("R1", "R2", "R3"):      # выброшено или пользовательский разговор / mute
            return
        module = getattr(item, "module", None)
        if module is None or self.policies_of(module) is None:  # модуль без политик: фокус ему не назначается
            return
        data = getattr(item, "data", None) or {}
        urgency = getattr(item, "urgency", None)
        self.queue.push({
            "module": module, "kind": getattr(item, "kind", ""), "text": getattr(item, "text", "") or "",
            "priority": float(priority or 0.0), "urgency": urgency, "critical": (urgency or 0) >= CRITICAL_URGENCY,
            "confidence": data.get("category_conf"), "category": data.get("category"),
            "expires": getattr(item, "expires", None), "dedupe_key": getattr(item, "dedupe_key", None),
            "hint": data.get("policy_hint"), "hint_conf": data.get("policy_conf"),
            "focus_needed": data.get("focus_needed"), "focus_conf": data.get("focus_conf"),
        })

    # ── жизненный цикл ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self.state_path is not None:
            await asyncio.to_thread(self._load)
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._save()

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:                                    # менеджер фокуса не должен гасить ядро
                logger.exception("Сбой менеджера фокуса (цикл продолжается)")
            await asyncio.sleep(TICK_S)

    async def tick(self) -> None:
        """Один шаг: очередь входа, сроки, состояние фокуса, оценка очереди, возобновление задач."""
        async with self._lock:
            now = self.clock()
            self.queue.drain(now)
            self.queue.expire(now)
            self.queue.age(now)
            await self._check_holder(now)
            self._refresh_context(now)
            switched = await self._evaluate_queue(now)
            if not switched:
                await self._resume_next(now)
            self._prune(now)
            await self._save()

    # ── очередь (делегирование) ─────────────────────────────────────────────

    def _finish(self, ev: QueuedEvent, status: str, reason: str, now: float) -> None:
        self.queue.finish(ev, status, reason, now)

    def _defer(self, ev: QueuedEvent, resume: dict, reason: str, now: float) -> None:
        self.queue.defer(ev, resume, reason, now)

    def _refresh_context(self, now: float) -> None:
        """FOC-3: отложенные возвращаются на оценку при смене контекста (фокус, политики, «не беспокоить»)."""
        sig = self._context_sig()
        changed = sig != self._sig
        self._sig = sig
        self.queue.refresh(now, changed)

    def _context_sig(self) -> tuple:
        holder = (self.slot.module, self.slot.policy) if self.slot else None
        states = []
        for mod in sorted(self._modules_seen):
            conn = self.connection_of(mod)
            cur = (self.status_of(mod) or {}).get("active_policy")
            states.append((mod, conn is not None, cur))
        return (holder, self.pinned, self.dnd_all, tuple(sorted(self._quiet_modules())), tuple(states))

    def _prune(self, now: float) -> None:
        self.queue.prune(now)
        self.store.prune()

    # ── состояние модулей ───────────────────────────────────────────────────

    def _policy_table(self, module: str) -> dict[str, dict] | None:
        spec = self.policies_of(module)
        if spec is None:
            return None
        return {p["id"]: p for p in spec.get("policies") or [] if isinstance(p, dict) and "id" in p}

    def _default_active(self, module: str) -> str | None:
        table = self._policy_table(module) or {}
        return next((pid for pid, p in table.items() if p.get("focus") == "active"), None)

    def _current(self, module: str) -> dict | None:
        """Спецификация текущей политики модуля по его статусу (None, если модуль её не объявил)."""
        pid = (self.status_of(module) or {}).get("active_policy")
        table = self._policy_table(module) or {}
        return table.get(pid) if isinstance(pid, str) else None

    def _is_background(self, module: str, policy: str | None) -> bool:
        table = self._policy_table(module) or {}
        return policy in table and table[policy].get("focus") == "background"

    def _holder_priority(self, slot: Slot | None = None) -> float:
        slot = slot or self.slot
        return policy.holder_priority(slot.origin if slot is not None else None)

    # ── протокол фокуса ─────────────────────────────────────────────────────

    async def _check_holder(self, now: float) -> None:
        """Фокус держит модуль: если он отключился, ушёл в фон сам (задача кончилась) или сообщил блокировку."""
        slot = self.slot
        if slot is None:
            return
        if self.connection_of(slot.module) is None:
            self._checkpoint(slot, None, "paused", "модуль отключился", {"type": "module_connected", "module": slot.module}, now)
            self.slot = None
            self._log("release", module=slot.module, task=slot.task_id, reason="модуль отключился", status="paused")
            return
        if now - slot.since < GRACE_S:
            return
        st = self.status_of(slot.module) or {}
        blocked = st.get("blocked")
        if blocked:
            text = str(blocked)[:160]
            self._checkpoint(slot, None, "blocked", f"заблокировано: {text}", {"type": "unblocked", "module": slot.module}, now)
            self.slot = None
            self._log("release", module=slot.module, task=slot.task_id, reason=f"модуль сообщил блокировку: {text}",
                      status="blocked")
            return
        active = st.get("active_policy")
        if active is not None and self._is_background(slot.module, active):
            self.slot = None
            self._log("release", module=slot.module, task=slot.task_id, reason=f"модуль вернулся в фон («{active}»)",
                      status="finished")

    def _checkpoint(self, slot: Slot, payload: Any, status: str, reason: str, resume: dict, now: float) -> dict:
        """FOC-5: checkpoint задачи. Лежит в памяти (слот `<модуль>/scratch` рабочей памяти) и пишется на диск в tick()."""
        cp = self.store.create(slot, payload, status, reason, resume, now)
        self._log("checkpoint", module=slot.module, task=slot.task_id, policy=slot.policy, status=status, reason=reason)
        return cp

    async def _release(self, slot: Slot, *, reason: str, force: bool, event_id: str | None, now: float) -> tuple[bool, str]:
        """Шаги checkpoint и release: модуль отвечает на focus.release своим checkpoint. Отказ оставляет фокус у него."""
        conn = self.connection_of(slot.module)
        if conn is None:
            self._checkpoint(slot, None, "paused", "модуль отключился", {"type": "module_connected", "module": slot.module}, now)
            self.slot = None
            return True, ""
        try:
            value = await conn.request("focus.release", {"reason": reason, "task_id": slot.task_id, "force": force,
                                                         "event_id": event_id, "checkpoint": True},
                                       timeout=RELEASE_TIMEOUT_S)
        except ModuleError as e:
            return False, f"{e.code}: {e.message}"
        if not isinstance(value, dict) or value.get("ok") is False:
            err = value.get("error") if isinstance(value, dict) else None
            return False, str(err or "модуль не освободил фокус")
        payload = value.get("checkpoint") if isinstance(value.get("checkpoint"), dict) else None
        self._checkpoint(slot, payload, "paused", reason, {"type": "holder_free"}, now)
        self.slot = None
        self._log("release", module=slot.module, task=slot.task_id, reason=reason, status="released")
        return True, ""

    async def _switch(self, ev: QueuedEvent, now: float, *, origin: str) -> bool:
        """Протокол переключения для события (или просьбы пользователя). True - фокус передан."""
        module = ev.module
        pol = ev.policy_pref or self._default_active(module)
        table = self._policy_table(module) or {}
        if pol is None or table.get(pol, {}).get("focus") != "active":
            self._defer(ev, {"type": "context_changed"}, f"у модуля «{module}» нет активной политики", now)
            return False
        cur = self._current(module)
        if cur is not None and cur.get("transitions") and pol not in cur["transitions"]:
            self._defer(ev, {"type": "context_changed"}, f"переход «{cur['id']}» → «{pol}» не разрешён политикой", now)
            return False
        conn = self.connection_of(module)
        if conn is None:
            self._defer(ev, {"type": "context_changed"}, f"модуль «{module}» не подключён", now)
            return False
        refused = self._refused_at.get(module)
        if refused is not None and now - refused < REFUSE_BACKOFF_S:
            # не отпускаем текущий фокус ради модуля, который только что отказал: иначе прерванная задача
            # (игра на паузе) уходит и возвращается впустую
            self._defer(ev, {"type": "time", "at": refused + REFUSE_BACKOFF_S},
                        f"модуль «{module}» недавно отказал в переходе: повтор позже", now)
            return False
        slot = self.slot
        if slot is not None and slot.module == module:
            self._finish(ev, RESOLVED, "модуль уже в фокусе", now)
            return True
        if slot is not None:                                    # check interruption пройдена: отпускаем текущий фокус
            ok, why = await self._release(slot, reason=f"фокус нужен: {ev.text[:120]}", force=ev.critical,
                                          event_id=ev.id, now=now)
            if not ok:
                self._defer(ev, {"type": "context_changed"}, f"модуль «{slot.module}» не освободил фокус: {why}", now)
                return False
        task_id = new_id("t")
        goal = ev.text[:200]
        prev = slot.module if slot is not None else "пользователь"
        ev.status = PROCESSING
        try:
            value = await conn.request("policy.activate", {"policy": pol, "goal": goal, "task_id": task_id,
                                                           "event_id": ev.id, "priority": ev.priority,
                                                           "reason": f"фокус передан с «{prev}»"},
                                       timeout=ACTIVATE_TIMEOUT_S)
            err = "" if _confirmed(value, pol) else f"модуль не подтвердил политику «{pol}»"
        except ModuleError as e:
            err = f"{e.code}: {e.message}"
        if err:
            ev.status = PENDING
            self._refused_at[module] = now
            self._defer(ev, {"type": "time", "at": now + REFUSE_BACKOFF_S}, f"переход не удался: {err}", now)
            self._log("activate", module=module, policy=pol, status="failed", reason=err, event=ev.id)
            return False
        origin_slot = Slot(module=module, task_id=task_id, policy=pol, goal=goal, priority=0.0, since=now,
                           origin=origin, event_id=ev.id)
        origin_slot.priority = self._holder_priority(origin_slot)
        origin_slot.echelon = ev.echelon
        self.slot = origin_slot
        self._switch_times.append(now)
        self._finish(ev, RESOLVED, f"фокус передан модулю «{module}» ({pol})", now)
        self._log("switch", module=module, policy=pol, task=task_id, event=ev.id, previous=prev,
                  priority_before=round(ev.initial, 1), priority_after=round(ev.priority, 1), reason=ev.reason,
                  **decision_fields(ev))
        return True

    async def _evaluate_queue(self, now: float) -> bool:
        """FOC-2/FOC-4: очередь по приоритету. Не больше одного переключения за шаг."""
        switched = False
        for ev in self.queue.pending_sorted():
            if ev.status != PENDING:
                continue
            ev.last_eval = now
            if self.slot is not None and ev.module == self.slot.module:
                self._finish(ev, RESOLVED, "фокус у этого модуля: он обработает событие сам", now)
                continue
            if policy.needs_recheck(ev.category, ev.confidence, ev.rechecked):
                await self._recheck_danger(ev, now)
            why = self._ambiguity(ev, now)
            if why and not ev.llm_asked:
                ev.llm_asked = True
                await self._ask_llm(ev, now, why)
                if ev.status != PENDING:
                    continue
            ev.echelon = event_echelon(ev)
            action, reason, resume = self._gate(ev, now)
            if action == "defer":
                self._defer(ev, resume, reason, now)
                continue
            if switched:
                self._defer(ev, {"type": "context_changed"}, "в этом шаге фокус уже передан", now)
                continue
            switched = await self._switch(ev, now, origin="event")
        return switched

    def _gate(self, ev: QueuedEvent, now: float) -> tuple[str, str, dict]:
        """Детерминированные правила (FOC-4, FOC-6): состояние собирается здесь, решение чистое (domain/focus_policy)."""
        cur = self._current(ev.module)
        slot = self.slot
        return policy.gate(
            critical=ev.critical, once=ev.once, quiet=self.dnd_all or ev.module in self.dnd_modules,
            profile_quiet=ev.module in self._profile_quiet(),
            quiet_policy=cur["id"] if cur is not None and cur.get("quiet") else None,
            pinned=self.pinned, pinned_title=self.title_of(self.pinned) if self.pinned is not None else "",
            module=ev.module, busy=bool(self.busy_of is not None and self.busy_of()),
            focus_needed=ev.focus_needed, focus_conf=ev.focus_conf, priority=ev.priority, has_slot=slot is not None,
            slot_since=slot.since if slot is not None else 0.0, switch_times=self._switch_times, now=now,
            holder=self._holder_priority(),
            interruptible=slot is None or (self.status_of(slot.module) or {}).get("interruptible") is not False)

    # ── эшелоны решения (ТЗ 18, второй эшелон) ─────────────────────────────

    def _ambiguity(self, ev: QueuedEvent, now: float) -> str | None:
        """Почему событие неоднозначно (нужен второй эшелон) или None, если правила решают сами (ТЗ 18, FOC-7)."""
        if ev.critical or self.llm is None or ev.llm_used:
            return None
        others = ((o.id, o.module, o.status, o.critical, o.priority) for o in self.events.values())
        return policy.ambiguity(
            critical=ev.critical, has_llm=self.llm is not None, llm_used=ev.llm_used, confidence=ev.confidence,
            near=policy.near_threshold(ev.priority, self._holder_priority(), self.slot is not None),
            conflict=policy.hint_conflicts(ev.hint, ev.hint_conf, ev.priority, ev.focus_needed, ev.module),
            claimant=policy.has_claimant(ev.priority, ev.module, ev.id, others, PENDING), hint=ev.hint)

    def _echelon(self, ev: QueuedEvent) -> str:
        return event_echelon(ev)

    async def _recheck_danger(self, ev: QueuedEvent, now: float) -> None:
        """FOC-3: опасное событие с низкой уверенностью не понижается: быстрая перепроверка, без неё - считаем опасным."""
        ev.rechecked = True
        new, error = await self.dispatch.urgency(dispatch.llm_event(ev), LLM_TIMEOUT_S)
        if error:
            self._log("recheck", event=ev.id, module=ev.module, status="failed", reason=error)
        if isinstance(new, int) and not isinstance(new, bool) and 0 <= new <= 5:
            ev.urgency = new
            ev.critical = new >= CRITICAL_URGENCY
            ev.llm_used = True
            ev.echelon = event_echelon(ev)
            self._log("recheck", event=ev.id, module=ev.module, status="done", reason=f"срочность {new}", **decision_fields(ev))
            return
        before = ev.priority
        ev.base = max(ev.base, float(SWITCH_P))
        ev.priority = max(ev.priority, float(SWITCH_P))
        self._log("recheck", event=ev.id, module=ev.module, status="fallback", priority_before=round(before, 1),
                  priority_after=round(ev.priority, 1), reason="низкая уверенность опасного события: считаем важным")

    # ── LLM-диспетчер (FOC-7) ───────────────────────────────────────────────

    async def _ask_llm(self, ev: QueuedEvent, now: float, why: str = "") -> None:
        """Диспетчер Gemma предлагает операции; каждая проверяется и применяется здесь. Сбой, тайм-аут или ответ не в
        формате JSON - детерминированные правила (ничего не меняется)."""
        slot = self.slot
        ctx = dispatch.FocusDispatch.context(
            ev, now, waiting=self.queue.waiting(), tables=self._policy_table, holder=slot,
            holder_status=self.status_of(slot.module) if slot is not None else None,
            checkpoints=self.store.paused(), pin=self.pinned, dnd_all=self.dnd_all, dnd_modules=sorted(self.dnd_modules))
        res = await self.dispatch.consult(ctx, now, LLM_TIMEOUT_S)
        if res is None:
            return
        if res.status != "ok":
            self._last_llm = {"ts": now, "status": res.status, "error": res.error}
            reason = "ответ не JSON-объект" if res.status == "invalid" else (
                "тайм-аут диспетчера" if res.error == "тайм-аут" else res.error)
            self._log("llm", event=ev.id, module=ev.module, status=res.status, reason=reason, why=why)
            return
        ops, rejected, reason = res.ops, res.rejected, res.reason
        self._last_llm = {"ts": now, "status": "ok", "reason": reason, "accepted": ops, "rejected": rejected}
        self._log("llm", event=ev.id, module=ev.module, status="ok", reason=reason or "без пояснения",
                  accepted=ops, rejected=rejected, why=why, confidence=ev.confidence,
                  echelon=ECHELON_GEMMA if ops else ev.echelon)
        if ops:                                                 # решение диспетчера принято: эшелон Gemma
            ev.llm_used = True
            ev.echelon = event_echelon(ev)
        for op in ops:
            await self._apply_op(op, now)

    async def _apply_op(self, op: dict, now: float) -> None:
        typ = op["type"]
        if typ == "reprioritize_event":
            ev = self.events[op["event_id"]]
            before = ev.priority
            ev.priority = ev.base = float(op["priority"])
            self._log("llm_op", event=ev.id, module=ev.module, priority_before=round(before, 1),
                      priority_after=round(ev.priority, 1), reason=f"диспетчер: {typ}")
        elif typ == "defer_event":
            ev = self.events[op["event_id"]]
            self._defer(ev, op["resume_condition"], "диспетчер отложил событие", now)
        elif typ == "select_task":
            self.events[op["event_id"]].preferred = True
            self._log("llm_op", event=op["event_id"], reason="диспетчер: выбрать задачу первой")
        elif typ == "request_policy_transition":
            for ev in self.events.values():
                if ev.module == op["module"] and ev.status == PENDING:
                    ev.policy_pref = op["policy"]
                    ev.preferred = True
                    break
            self._log("llm_op", module=op["module"], policy=op["policy"], reason="диспетчер: политика для перехода")
        elif typ == "pause_task":
            slot = self.slot
            if slot is not None and slot.module == op["module"]:
                ok, why = await self._release(slot, reason="пауза по решению диспетчера", force=False, event_id=None, now=now)
                self._log("llm_op", module=slot.module, status="ok" if ok else "failed", reason=why or "пауза задачи")
        elif typ == "resume_task":
            self.checkpoints[op["task_id"]]["resume_now"] = True
            self._log("llm_op", task=op["task_id"], reason="диспетчер: возобновить задачу первой")
        elif typ == "create_subgoal":
            cp = self.checkpoints[op["task_id"]]
            cp["subgoals"].append(op["goal"])
            self.store.touch()
            self._log("llm_op", task=op["task_id"], reason=f"диспетчер: подцель «{op['goal'][:80]}»")

    # ── возобновление (FOC-4, resume/schedule) ──────────────────────────────

    async def _resume_next(self, now: float) -> None:
        if self.slot is not None:
            return
        if any(e.status == PENDING and e.priority >= SWITCH_P for e in self.events.values()):
            return                                              # важнее ждущего события задача не возобновляется
        for cp in self.store.resumable():
            if now - cp["created"] > CHECKPOINT_MAX_AGE_S:
                cp["status"] = "stale"
                self.store.touch()
                self._log("checkpoint", module=cp["module"], task=cp["task_id"], status="stale",
                          reason="задача слишком давно на паузе, checkpoint устарел")
                continue
            if self.pinned is not None and cp["module"] != self.pinned:
                continue
            conn = self.connection_of(cp["module"])
            if conn is None:
                continue
            if cp["status"] == "blocked" and (self.status_of(cp["module"]) or {}).get("blocked"):
                continue
            if await self._resume(cp, conn, now):
                return

    async def _resume(self, cp: dict, conn: Any, now: float) -> bool:
        body = {"task_id": cp["task_id"], "policy": cp["policy"], "goal": cp["goal"], "checkpoint": cp["payload"],
                "subgoals": cp["subgoals"]}
        value: Any = None
        try:
            value = await conn.request("focus.resume", body, timeout=RESUME_TIMEOUT_S)
        except ModuleError as e:
            if e.code == "unknown":                             # модуль без focus.resume (ТЗ 11e): возврат через policy.activate
                try:
                    value = await conn.request("policy.activate", {**body, "restored": True,
                                                                   "reason": "возобновление задачи"},
                                               timeout=ACTIVATE_TIMEOUT_S)
                except ModuleError as e2:
                    self._log("resume", module=cp["module"], task=cp["task_id"], status="failed", reason=e2.message)
            else:
                self._log("resume", module=cp["module"], task=cp["task_id"], status="failed", reason=e.message)
        if _confirmed(value, cp["policy"]):
            self.slot = Slot(module=cp["module"], task_id=cp["task_id"], policy=cp["policy"], goal=cp["goal"],
                             priority=0.0, since=now, origin="resume", echelon=ECHELON_RULES)
            self.slot.priority = self._holder_priority(self.slot)
            cp["status"], cp["resume_now"] = "resumed", False
            self._switch_times.append(now)
            self.store.touch()
            self._log("resume", module=cp["module"], task=cp["task_id"], policy=cp["policy"], status="resumed",
                      reason=cp["reason"])
            return True
        cp["attempts"] += 1
        self.store.touch()
        if cp["attempts"] >= RESUME_RETRIES:
            cp["status"] = "stale"
            self._log("resume", module=cp["module"], task=cp["task_id"], status="stale",
                      reason=f"модуль не вернул задачу за {RESUME_RETRIES} попытки")
        return False

    # ── команды пользователя (FOC-6) ───────────────────────────────────────

    async def request(self, module: str, *, policy: str | None = None, goal: str = "", source: str = "user") -> dict:
        """Просьба пользователя: передать фокус модулю. Обходит дребезг и правила «не беспокоить», но не отказ модуля."""
        async with self._lock:
            return await self._request_locked(module, policy=policy, goal=goal, source=source)

    async def _request_locked(self, module: str, *, policy: str | None, goal: str, source: str) -> dict:
        now = self.clock()
        if self._policy_table(module) is None:
            return {"ok": False, "reason": f"модуль «{module}» не объявляет политик"}
        pol = policy or self._default_active(module)
        if self.slot is not None and self.slot.module == module and (pol is None or pol == self.slot.policy):
            return {"ok": True, "already": True, "policy": self.slot.policy}
        ev = QueuedEvent(id=new_id("fe"), module=module, kind=f"{module}.user_request",
                         text=goal or f"просьба пользователя: фокус модулю «{self.title_of(module)}»",
                         priority=float(USER_PRIORITY), base=float(USER_PRIORITY), created=now, critical=False,
                         initial=float(USER_PRIORITY), last_eval=now, updated=now, user=True, policy_pref=pol)
        self.queue.add(ev)
        self._log("request", module=module, source=source, policy=pol, event=ev.id, reason=ev.text[:160])
        ok = await self._switch(ev, now, origin="user")
        self.store.touch()
        return {"ok": ok, "reason": ev.reason, "policy": pol if ok else None}

    async def release_holder(self, *, reason: str = "user", force: bool = False) -> dict:
        """Освободить слот фокуса по просьбе (выход из среды, ТЗ 32): модуль отдаёт checkpoint. Отказ модуля оставляет фокус."""
        async with self._lock:
            if self.slot is None:
                return {"ok": True, "already": True}
            ok, why = await self._release(self.slot, reason=reason, force=force, event_id=None, now=self.clock())
            self.store.touch()
            return {"ok": ok, "reason": why}

    async def pin(self, module: str | None) -> dict:
        """Закрепить фокус за модулем (или снять закрепление, module=None): другие модули не переключают фокус, кроме
        критических событий. Закрепление за модулем без фокуса сразу передаёт ему фокус."""
        async with self._lock:
            self.pinned = module or None
            self.store.touch()
            self._log("pin", module=self.pinned or "", reason="фокус закреплён" if self.pinned else "закрепление снято")
            if self.pinned is None:
                return {"ok": True, "pin": None}
            result: dict = {"ok": True, "pin": self.pinned}
            if self.slot is None or self.slot.module != self.pinned:
                result["request"] = await self._request_locked(self.pinned, policy=None, goal="закреплено пользователем",
                                                               source="pin")
            return result

    async def set_dnd(self, module: str, on: bool) -> dict:
        """«Не беспокоить»: для всего агента (module='all') или одного модуля. Сообщения копятся, фокус не переключают.
        Для всего агента пишется в общую настройку ядра (одно состояние с дашбордом и голосом); разовые исключения не
        сбрасываются."""
        async with self._lock:
            if module == "all":
                if self._set_dnd_all is not None:
                    self._set_dnd_all(bool(on))
                else:
                    self._dnd_local = bool(on)
            elif on:
                self.dnd_modules.add(module)
            else:
                self.dnd_modules.discard(module)
            self.store.touch()
            self._log("dnd", module=module, reason="не беспокоить " + ("включено" if on else "выключено"))
            return {"ok": True, "all": self.dnd_all, "modules": sorted(self.dnd_modules),
                    "profile": sorted(self._profile_quiet())}

    async def allow_once(self, module: str) -> dict:
        """Разовое исключение: следующее обычное событие модуля проходит режим «не беспокоить». Сам режим не отменяется."""
        async with self._lock:
            self._once.add(module)
            self._log("once", module=module, reason="разовое исключение из режима «не беспокоить»")
            return {"ok": True, "module": module}

    # ── журнал и сохранение ─────────────────────────────────────────────────

    def _log(self, type_: str, **fields: Any) -> None:
        """FOC-8: аудит. Журнал в памяти (для дашборда) и в общий журнал событий. Поле вида события - `event_kind`
        (имя `kind` занято параметром журнала событий)."""
        if "kind" in fields:
            fields["event_kind"] = fields.pop("kind")
        record = {"ts": round(self.clock(), 3), "type": type_, **fields}
        self._journal.append(record)
        text = str(fields.get("reason") or type_)
        try:
            events_log.log_nowait("focus", text[:200], **{k: v for k, v in record.items() if k not in ("text", "type")},
                                  focus_type=type_)
        except Exception:                                       # журнал не должен ронять переходы
            logger.debug("журнал фокуса не записан", exc_info=True)

    def _state_dict(self) -> dict:
        return {"v": 1, "pin": self.pinned, "dnd_all": self.dnd_all, "dnd_modules": sorted(self.dnd_modules),
                "checkpoints": list(self.checkpoints.values()), "queue": self.queue.dump()}

    async def _save(self) -> None:
        if self.state_path is None or not self.store.dirty:
            return
        await self.store.save(self._state_dict())

    def _load(self) -> None:
        data = self.store.read()
        if data is None:
            return
        self.pinned = data.get("pin") if isinstance(data.get("pin"), str) else None
        self._dnd_local = bool(data.get("dnd_all"))
        self.dnd_modules = {m for m in data.get("dnd_modules") or [] if isinstance(m, str)}
        self.queue.load(data.get("queue"), self.clock())
        self.store.dirty = False

    # ── снимок для дашборда (FOC-9) ─────────────────────────────────────────

    def snapshot(self) -> dict:
        holder = self.slot.public(self.pinned) if self.slot is not None else None
        policies = {}
        for mod in sorted(self._modules_seen | ({self.slot.module} if self.slot else set())):
            table = self._policy_table(mod)
            if table is None:
                continue
            cur = self._current(mod)
            policies[mod] = {"active": cur["id"] if cur else None, "title": self.title_of(mod),
                             "default": (self.policies_of(mod) or {}).get("default_policy"),
                             "policies": [{"id": p["id"], "title": p.get("title", p["id"]), "focus": p.get("focus"),
                                           "transitions": p.get("transitions") or [], "quiet": bool(p.get("quiet"))}
                                          for p in table.values()],
                             "scratch": f"{mod}/scratch"}
        return {
            "holder": holder, "pin": self.pinned,
            "dnd": {"all": self.dnd_all, "modules": sorted(self.dnd_modules), "profile": sorted(self._profile_quiet())},
            "once": sorted(self._once),
            "queue": [e.public() for e in sorted(self.events.values(), key=lambda e: -e.priority)
                      if e.status in (PENDING, DEFERRED)][:30],
            "checkpoints": [{k: v for k, v in c.items() if k != "payload"} for c in
                            sorted(self.checkpoints.values(), key=lambda c: -c["created"])][:20],
            "journal": list(self._journal)[-80:],
            "switches": [r for r in list(self._journal) if r["type"] in ("switch", "release", "resume")][-10:],
            "llm": {"enabled": self.llm is not None, "last": self._last_llm},
            "echelons": {"rules": ECHELON_RULES, "gliner": ECHELON_GLINER, "gemma": ECHELON_GEMMA},
            "policies": policies,
        }
