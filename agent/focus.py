"""Менеджер фокуса (ТЗ 18, FOC-1…FOC-9): единственный слот фокуса над модулями, очередь событий, протокол переключения,
checkpoint задач, дребезг, пользовательские «закрепить», «не беспокоить» и разовое исключение, LLM-диспетчер для
неоднозначных случаев, аудит.

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
import json
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import events_log
import focus_dispatch as dispatch
from domain import focus_policy as policy
from domain.focus_policy import ECHELON_GEMMA, ECHELON_GLINER, ECHELON_RULES, SWITCH_P, MARGIN, CONF_LOW, CONF_HIGH
from module_hub import ModuleError

logger = logging.getLogger("focus")

# ── пороги и сроки ───────────────────────────────────────────────────────────
CRITICAL_URGENCY = 5           # срочность 5 - критическое: без дребезга, без LLM, без ожидания
USER_PRIORITY = 95             # просьба пользователя (закрепить, «возьми фокус»)
HOLDER_PRIORITY_EVENT = 60     # фокус, взятый по событию, держит приоритет 60: обычные события его не сдвигают
GRACE_S = 5.0                  # после передачи фокуса статусу модуля не верим этот срок (он может отстать)
RECHECK_S = 30.0               # отложенные события переоцениваются раз в RECHECK_S (и при смене контекста)
EVENT_TTL_S = 1800.0           # отложенное событие, которое так и не стало важным, истекает через полчаса
DEDUPE_S = 60.0                # одинаковые события одного модуля в это окно склеиваются
RECHECK_CONF = 0.5             # опасное событие с уверенностью классификатора ниже этого перепроверяется
DANGER_CATEGORIES = ("danger", "death_injury", "disaster")
CHECKPOINT_MAX_AGE_S = 6 * 3600.0
CHECKPOINT_MAX_BYTES = 16 * 1024
CHECKPOINTS_KEEP = 50
RESUME_RETRIES = 3
REFUSE_BACKOFF_S = 60.0         # модуль отказал в переходе: повтор к нему не раньше чем через столько (не трогая фокус)
RELEASE_TIMEOUT_S = 15.0
ACTIVATE_TIMEOUT_S = 20.0
RESUME_TIMEOUT_S = 20.0
LLM_TIMEOUT_S = 5.0
RESOLVED_KEEP_S = 600.0
JOURNAL_MAX = 300
TICK_S = 0.5
STATE_FILE = "focus.json"
MAX_POLICY_OPTIONS = 12


PENDING = "pending"            # ждёт оценки
DEFERRED = "deferred"          # отложено, ждёт условия возобновления
PROCESSING = "processing"      # идёт переход фокуса
RESOLVED = "resolved"          # обработано (фокус передан или модуль сам справится)
EXPIRED = "expired"            # срок вышел
DISCARDED = "discarded"        # слито с другим событием

_EVENT_ALIVE = (PENDING, DEFERRED, PROCESSING)

Connect = Callable[[str], Any]


@dataclass
class QueuedEvent:
    """Событие в очереди (ТЗ 18, FOC-3): состояние, приоритет до и после, условие возобновления."""

    id: str
    module: str
    kind: str
    text: str
    priority: float
    base: float
    created: float
    urgency: int | None = None
    critical: bool = False
    confidence: float | None = None
    category: str | None = None
    expires: float | None = None
    dedupe_key: str | None = None
    count: int = 1
    status: str = PENDING
    reason: str = ""
    resume: dict = field(default_factory=dict)
    initial: float = 0.0
    last_eval: float = 0.0
    updated: float = 0.0
    user: bool = False
    once: bool = False
    preferred: bool = False
    policy_pref: str | None = None
    llm_asked: bool = False
    rechecked: bool = False
    hint: str | None = None              # подсказка GLiNER: «модуль/политика» или NONE_HINT
    hint_conf: float = 0.0
    focus_needed: bool | None = None     # GLiNER: нужен ли фокус
    focus_conf: float = 0.0
    echelon: str = ECHELON_RULES         # какой эшелон решил (см. ECHELON_*)
    llm_used: bool = False               # диспетчер Gemma ответил операцией или перепроверкой

    def public(self) -> dict:
        return {"id": self.id, "module": self.module, "kind": self.kind, "text": self.text[:160],
                "status": self.status, "priority": round(self.priority, 1), "initial": round(self.initial, 1),
                "critical": self.critical, "count": self.count, "reason": self.reason, "resume": dict(self.resume),
                "echelon": self.echelon, "confidence": self.confidence, "hint": self.hint}


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


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _json_safe(payload: Any) -> dict | None:
    """Checkpoint модуля: только JSON и не больше CHECKPOINT_MAX_BYTES; иначе сохраняется пометка."""
    if not isinstance(payload, dict):
        return None
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"note": "checkpoint модуля не сериализуется"}
    if len(text.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        return {"note": "checkpoint модуля больше лимита и не сохранён целиком"}
    return json.loads(text)


_confirmed = policy.confirmed                               # domain/focus_policy.py
_hint = policy.hint_value                                   # domain/focus_policy.py


def _decision_fields(ev: QueuedEvent) -> dict:
    """Эшелон и уверенность для записи решения (ТЗ 18, FOC-8, аудит)."""
    return {"echelon": ev.echelon, "confidence": ev.confidence, "hint": ev.hint}


def _write_json(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


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
        self.state_path = state_path
        self.llm = llm
        self.busy_of = busy_of
        self.recheck = recheck
        self.title_of = title_of or (lambda mid: mid)
        self.slot: Slot | None = None
        self.pinned: str | None = None
        self._dnd_all_of = dnd_all_of
        self._set_dnd_all = set_dnd_all
        self._profile_quiet_of = profile_quiet_of
        self._modules_of = modules_of
        self._dnd_local = False
        self.dnd_modules: set[str] = set()
        self._once: set[str] = set()
        self.events: dict[str, QueuedEvent] = {}
        self.checkpoints: dict[str, dict] = {}
        self._inbox: list[dict] = []
        self._journal: deque = deque(maxlen=JOURNAL_MAX)
        self._switch_times: deque = deque(maxlen=64)
        self._refused_at: dict[str, float] = {}      # модуль -> когда он последний раз отказал в переходе
        self._modules_seen: set[str] = set()
        self._sig: tuple | None = None
        self._last_llm: dict | None = None
        self._dirty = False
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

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
        self._inbox.append({
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
            self._drain(now)
            self._expire(now)
            self._age(now)
            await self._check_holder(now)
            self._refresh_context(now)
            switched = await self._evaluate_queue(now)
            if not switched:
                await self._resume_next(now)
            self._prune(now)
            await self._save()

    # ── очередь ─────────────────────────────────────────────────────────────

    def _drain(self, now: float) -> None:
        """ATT-6 и FOC-3: одинаковое событие в окне DEDUPE_S склеивается с ожидающим, остальные становятся очередью."""
        batch, self._inbox = self._inbox, []
        for rec in batch:
            module = rec["module"]
            self._modules_seen.add(module)
            key = rec["dedupe_key"] or rec["kind"]
            twin = next((e for e in self.events.values() if e.status in (PENDING, DEFERRED) and e.module == module
                         and (e.dedupe_key or e.kind) == key and now - e.created <= DEDUPE_S), None)
            if twin is not None:
                twin.count += 1
                twin.text = rec["text"]
                twin.base = max(twin.base, rec["priority"])
                twin.critical = twin.critical or rec["critical"]
                twin.updated = now
                self._log("merge", event=twin.id, module=module, count=twin.count, priority=round(twin.base, 1))
                continue
            ev = QueuedEvent(id=_new_id("fe"), module=module, kind=rec["kind"], text=rec["text"], priority=rec["priority"],
                             base=rec["priority"], created=now, urgency=rec["urgency"], critical=rec["critical"],
                             confidence=rec["confidence"], category=rec["category"], expires=rec["expires"],
                             dedupe_key=rec["dedupe_key"], initial=rec["priority"], last_eval=now, updated=now,
                             hint=_hint(rec.get("hint")), hint_conf=float(rec.get("hint_conf") or 0.0),
                             focus_needed=rec.get("focus_needed") if isinstance(rec.get("focus_needed"), bool) else None,
                             focus_conf=float(rec.get("focus_conf") or 0.0))
            if not ev.critical and module in self._once:
                self._once.discard(module)
                ev.once = True
            ev.echelon = self._echelon(ev)
            self.events[ev.id] = ev
            self._dirty = True
            self._log("enqueue", event=ev.id, module=module, kind=rec["kind"], priority_before=round(rec["priority"], 1),
                      critical=ev.critical, status=PENDING, text=ev.text, **_decision_fields(ev))

    def _expire(self, now: float) -> None:
        for ev in self.events.values():
            if ev.status not in (PENDING, DEFERRED):
                continue
            if ev.expires is not None and now > ev.expires:
                self._finish(ev, EXPIRED, "срок события вышел", now)
            elif ev.status == DEFERRED and now - ev.created > EVENT_TTL_S:
                self._finish(ev, EXPIRED, "отложено дольше срока ожидания", now)

    def _age(self, now: float) -> None:
        """FOC-3, защита от вечного откладывания: ожидание поднимает приоритет до AGING_CAP."""
        for ev in self.events.values():
            if ev.status not in (PENDING, DEFERRED) or ev.critical:
                continue
            aged = policy.aged_priority(ev.base, now - ev.created)
            if aged > ev.priority:
                ev.priority = aged
                self._log("aging", event=ev.id, module=ev.module, priority_before=round(ev.base, 1),
                          priority_after=round(aged, 1), reason="ожидание поднимает приоритет")

    def _finish(self, ev: QueuedEvent, status: str, reason: str, now: float) -> None:
        ev.status, ev.reason, ev.updated = status, reason, now
        ev.resume = {}
        self._dirty = True
        self._log("resolve" if status == RESOLVED else status, event=ev.id, module=ev.module, kind=ev.kind,
                  priority_before=round(ev.initial, 1), priority_after=round(ev.priority, 1), reason=reason,
                  status=status, **_decision_fields(ev))

    def _defer(self, ev: QueuedEvent, resume: dict, reason: str, now: float) -> None:
        """FOC-3: отложить с причиной. Повтор той же причины не пишет журнал заново."""
        ev.last_eval = now
        if ev.status == DEFERRED and ev.reason == reason:
            return
        ev.status, ev.reason, ev.resume, ev.updated = DEFERRED, reason, dict(resume), now
        self._dirty = True
        self._log("defer", event=ev.id, module=ev.module, kind=ev.kind, priority_before=round(ev.initial, 1),
                  priority_after=round(ev.priority, 1), reason=reason, resume=resume, status=DEFERRED,
                  **_decision_fields(ev))

    def _refresh_context(self, now: float) -> None:
        """FOC-3: отложенные события возвращаются на оценку при смене контекста (фокус, политики, «не беспокоить») и раз
        в RECHECK_S; «время» возвращает, когда наступило."""
        sig = self._context_sig()
        changed = sig != self._sig
        self._sig = sig
        for ev in self.events.values():
            if ev.status != DEFERRED:
                continue
            kind = ev.resume.get("type")
            if kind == "time":
                if now >= float(ev.resume.get("at") or 0):
                    ev.status = PENDING
            elif changed or now - ev.last_eval >= RECHECK_S:
                ev.status = PENDING

    def _context_sig(self) -> tuple:
        holder = (self.slot.module, self.slot.policy) if self.slot else None
        states = []
        for mod in sorted(self._modules_seen):
            conn = self.connection_of(mod)
            cur = (self.status_of(mod) or {}).get("active_policy")
            states.append((mod, conn is not None, cur))
        return (holder, self.pinned, self.dnd_all, tuple(sorted(self._quiet_modules())), tuple(states))

    def _prune(self, now: float) -> None:
        for eid in [e.id for e in self.events.values() if e.status not in _EVENT_ALIVE
                    and now - e.updated > RESOLVED_KEEP_S]:
            del self.events[eid]
        if len(self.checkpoints) > CHECKPOINTS_KEEP:
            done = sorted((c for c in self.checkpoints.values() if c["status"] in ("resumed", "stale")),
                          key=lambda c: c["created"])
            for cp in done[:len(self.checkpoints) - CHECKPOINTS_KEEP]:
                self.checkpoints.pop(cp["task_id"], None)

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
        if slot is None:
            return 0.0
        return USER_PRIORITY if slot.origin == "user" else HOLDER_PRIORITY_EVENT

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
        cp = {"task_id": slot.task_id, "module": slot.module, "policy": slot.policy, "goal": slot.goal,
              "priority": slot.priority, "origin": slot.origin, "status": status, "reason": reason, "resume": resume,
              "created": now, "attempts": 0, "subgoals": [], "payload": _json_safe(payload),
              "scratch": f"{slot.module}/scratch"}
        self.checkpoints[slot.task_id] = cp
        self._dirty = True
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
        task_id = _new_id("t")
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
                  **_decision_fields(ev))
        return True

    async def _evaluate_queue(self, now: float) -> bool:
        """FOC-2/FOC-4: очередь по приоритету. Не больше одного переключения за шаг."""
        switched = False
        pending = sorted((e for e in self.events.values() if e.status == PENDING),
                         key=lambda e: (not e.preferred, -e.priority, e.created))
        for ev in pending:
            if ev.status != PENDING:
                continue
            ev.last_eval = now
            if self.slot is not None and ev.module == self.slot.module:
                self._finish(ev, RESOLVED, "фокус у этого модуля: он обработает событие сам", now)
                continue
            if ev.category in DANGER_CATEGORIES and ev.confidence is not None and ev.confidence < RECHECK_CONF \
                    and not ev.rechecked:
                await self._recheck_danger(ev, now)
            why = self._ambiguity(ev, now)
            if why and not ev.llm_asked:
                ev.llm_asked = True
                await self._ask_llm(ev, now, why)
                if ev.status != PENDING:
                    continue
            ev.echelon = self._echelon(ev)
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
        """Детерминированные правила (FOC-4, FOC-6): что делать с событием. Возвращает (действие, причина, условие)."""
        ctx = {"type": "context_changed"}
        if ev.critical:
            return "switch", "критическое событие: переключение без ожидания", ctx
        if not ev.once:
            if self.dnd_all or ev.module in self.dnd_modules:
                return "defer", "не беспокоить: режим пользователя", {"type": "context_changed"}
            if ev.module in self._profile_quiet():
                return "defer", "профиль не даёт переключать фокус этому модулю", ctx
            cur = self._current(ev.module)
            if cur is not None and cur.get("quiet"):
                return "defer", f"политика «{cur['id']}» не переключает фокус: сообщение сохранено", ctx
        if self.pinned is not None and ev.module != self.pinned:
            return "defer", f"фокус закреплён за «{self.title_of(self.pinned)}»", {"type": "unpin"}
        if self.busy_of is not None and self.busy_of():
            return "defer", "пользователь в разговоре", ctx
        if ev.focus_needed is False and ev.focus_conf >= CONF_HIGH:
            return "defer", "классификатор: событие не требует фокуса", ctx
        if self.slot is None:
            if ev.priority >= SWITCH_P:
                return "switch", f"приоритет {round(ev.priority)} не ниже порога {SWITCH_P}", ctx
            return "defer", f"приоритет {round(ev.priority)} ниже порога переключения {SWITCH_P}", ctx
        damp = policy.dampening(now, self.slot.since, self._switch_times)
        if damp == "short":
            return "defer", "дребезг: мало времени в текущем фокусе", ctx
        if damp == "rate":
            return "defer", "дребезг: лимит переключений в минуту", ctx
        holder = self._holder_priority()
        if not policy.clears_focus(ev.priority, holder):
            return "defer", f"не выше текущего фокуса на запас {MARGIN}", ctx
        st = self.status_of(self.slot.module) or {}
        if st.get("interruptible") is False:
            return "defer", "модуль в фокусе сообщил небезопасную точку", ctx
        return "switch", f"приоритет {round(ev.priority)} выше фокуса {round(holder)} на запас", ctx

    # ── эшелоны решения (ТЗ 18, второй эшелон) ─────────────────────────────

    def _near(self, ev: QueuedEvent) -> bool:
        """Близко к порогу переключения или к приоритету фокуса: по правилам не решается однозначно."""
        return policy.near_threshold(ev.priority, self._holder_priority(), self.slot is not None)

    def _conflict(self, ev: QueuedEvent) -> bool:
        """Конфликт подсказки: классификатор указывает на другой модуль или говорит «фокус не нужен» при высокой важности."""
        return policy.hint_conflicts(ev.hint, ev.hint_conf, ev.priority, ev.focus_needed, ev.module)

    def _claimant(self, ev: QueuedEvent) -> bool:
        """Два претендента на фокус с близкой важностью от разных модулей."""
        others = ((o.id, o.module, o.status, o.critical, o.priority) for o in self.events.values())
        return policy.has_claimant(ev.priority, ev.module, ev.id, others, PENDING)

    def _ambiguity(self, ev: QueuedEvent, now: float) -> str | None:
        """Почему событие неоднозначно (нужен второй эшелон) или None, если правила решают сами (ТЗ 18, FOC-7):
        низкая уверенность GLiNER, близость к порогу без уверенного ответа, конфликт подсказки, два претендента.
        Критические события, отсутствие LLM и уже отвеченные диспетчером не спрашиваем."""
        if ev.critical or self.llm is None or ev.llm_used:
            return None
        conf = ev.confidence
        if conf is not None and conf < CONF_LOW:
            return f"низкая уверенность классификатора ({conf:.2f})"
        if self._near(ev) and (conf is None or conf < CONF_HIGH):
            return "у порога переключения или у фокуса, без уверенного ответа классификатора"
        if self._conflict(ev):
            return f"конфликт подсказки: классификатор указывает «{ev.hint}»"
        if self._claimant(ev):
            return "два претендента на фокус с близкой важностью"
        return None

    def _echelon(self, ev: QueuedEvent) -> str:
        """Какой эшелон решил: Gemma (если ответила операцией или перепроверкой), иначе GLiNER (если давал ответ), иначе правила."""
        return policy.echelon(ev.llm_used, ev.critical,
                              ev.confidence is not None or ev.hint is not None or ev.focus_needed is not None)

    async def _recheck_danger(self, ev: QueuedEvent, now: float) -> None:
        """FOC-3: опасное событие с низкой уверенностью не понижается: быстрая перепроверка, без неё - считаем опасным."""
        ev.rechecked = True
        new = None
        if self.recheck is not None:
            try:
                new = await asyncio.wait_for(self.recheck(self._llm_event(ev)), LLM_TIMEOUT_S)
            except Exception as e:                              # тайм-аут и сбой перепроверки - правило ниже
                self._log("recheck", event=ev.id, module=ev.module, status="failed", reason=str(e)[:160] or type(e).__name__)
        if isinstance(new, int) and not isinstance(new, bool) and 0 <= new <= 5:
            ev.urgency = new
            ev.critical = new >= CRITICAL_URGENCY
            ev.llm_used = True
            ev.echelon = self._echelon(ev)
            self._log("recheck", event=ev.id, module=ev.module, status="done", reason=f"срочность {new}", **_decision_fields(ev))
            return
        before = ev.priority
        ev.base = max(ev.base, float(SWITCH_P))
        ev.priority = max(ev.priority, float(SWITCH_P))
        self._log("recheck", event=ev.id, module=ev.module, status="fallback", priority_before=round(before, 1),
                  priority_after=round(ev.priority, 1), reason="низкая уверенность опасного события: считаем важным")

    # ── LLM-диспетчер (FOC-7) ───────────────────────────────────────────────

    @staticmethod
    def _llm_event(ev: QueuedEvent) -> dict:
        return {"id": ev.id, "module": ev.module, "kind": ev.kind, "text": ev.text[:200], "priority": round(ev.priority, 1),
                "critical": ev.critical, "confidence": ev.confidence, "category": ev.category, "hint": ev.hint,
                "hint_conf": round(ev.hint_conf, 3), "focus_needed": ev.focus_needed}

    def _llm_context(self, ev: QueuedEvent, now: float) -> dict:
        """Структурированное состояние для диспетчера: очередь, фокус, политики, задачи. Не полный снимок мира."""
        queue = sorted((e for e in self.events.values() if e.status in (PENDING, DEFERRED)),
                       key=lambda e: -e.priority)[:10]
        modules = {}
        for mod in {e.module for e in queue} | ({self.slot.module} if self.slot else set()):
            table = self._policy_table(mod) or {}
            modules[mod] = {"policies": {pid: p.get("focus") for pid, p in table.items()}}
        holder = None
        if self.slot is not None:
            st = self.status_of(self.slot.module) or {}
            holder = {"module": self.slot.module, "policy": self.slot.policy, "goal": self.slot.goal[:160],
                      "interruptible": st.get("interruptible") is not False}
        cps = [c for c in self.checkpoints.values() if c["status"] in ("paused", "blocked")][:5]
        return {
            "now": now, "event": self._llm_event(ev),
            "events": {e.id: {"module": e.module, "status": e.status, "critical": e.critical,
                              "priority": round(e.priority, 1)} for e in queue},
            "modules": modules, "holder": holder,
            "checkpoints": {c["task_id"]: {"module": c["module"], "status": c["status"], "goal": c["goal"][:120],
                                           "subgoals": len(c["subgoals"])} for c in cps},
            "pin": self.pinned, "dnd": {"all": self.dnd_all, "modules": sorted(self.dnd_modules)},
        }

    async def _ask_llm(self, ev: QueuedEvent, now: float, why: str = "") -> None:
        """Диспетчер Gemma предлагает операции; каждая проверяется и применяется здесь. Сбой, тайм-аут или ответ не в
        формате JSON - детерминированные правила (ничего не меняется)."""
        ctx = self._llm_context(ev, now)
        if self.llm is None:
            return
        try:
            raw = await asyncio.wait_for(self.llm(ctx), LLM_TIMEOUT_S)
        except asyncio.TimeoutError:
            self._last_llm = {"ts": now, "status": "unavailable", "error": "тайм-аут"}
            self._log("llm", event=ev.id, module=ev.module, status="unavailable", reason="тайм-аут диспетчера", why=why)
            return
        except ValueError as e:                                 # ответ не строгий JSON: как сбой, правила
            self._last_llm = {"ts": now, "status": "invalid", "error": str(e)[:160]}
            self._log("llm", event=ev.id, module=ev.module, status="invalid", reason="ответ не JSON-объект", why=why)
            return
        except Exception as e:
            self._last_llm = {"ts": now, "status": "unavailable", "error": str(e)[:160] or type(e).__name__}
            self._log("llm", event=ev.id, module=ev.module, status="unavailable", reason=self._last_llm["error"], why=why)
            return
        ops, rejected, reason = dispatch.validate(raw, {**ctx, "now": now})
        self._last_llm = {"ts": now, "status": "ok", "reason": reason, "accepted": ops, "rejected": rejected}
        self._log("llm", event=ev.id, module=ev.module, status="ok", reason=reason or "без пояснения",
                  accepted=ops, rejected=rejected, why=why, confidence=ev.confidence,
                  echelon=ECHELON_GEMMA if ops else ev.echelon)
        if ops:                                                 # решение диспетчера принято: эшелон Gemma
            ev.llm_used = True
            ev.echelon = self._echelon(ev)
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
            self._dirty = True
            self._log("llm_op", task=op["task_id"], reason=f"диспетчер: подцель «{op['goal'][:80]}»")

    # ── возобновление (FOC-4, resume/schedule) ──────────────────────────────

    async def _resume_next(self, now: float) -> None:
        if self.slot is not None:
            return
        if any(e.status == PENDING and e.priority >= SWITCH_P for e in self.events.values()):
            return                                              # важнее ждущего события задача не возобновляется
        cands = [c for c in self.checkpoints.values() if c["status"] in ("paused", "blocked")]
        cands.sort(key=lambda c: (not c.get("resume_now"), -c["priority"], c["created"]))
        for cp in cands:
            if now - cp["created"] > CHECKPOINT_MAX_AGE_S:
                cp["status"] = "stale"
                self._dirty = True
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
            self._dirty = True
            self._log("resume", module=cp["module"], task=cp["task_id"], policy=cp["policy"], status="resumed",
                      reason=cp["reason"])
            return True
        cp["attempts"] += 1
        self._dirty = True
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
        ev = QueuedEvent(id=_new_id("fe"), module=module, kind=f"{module}.user_request",
                         text=goal or f"просьба пользователя: фокус модулю «{self.title_of(module)}»",
                         priority=float(USER_PRIORITY), base=float(USER_PRIORITY), created=now, critical=False,
                         initial=float(USER_PRIORITY), last_eval=now, updated=now, user=True, policy_pref=pol)
        self.events[ev.id] = ev
        self._log("request", module=module, source=source, policy=pol, event=ev.id, reason=ev.text[:160])
        ok = await self._switch(ev, now, origin="user")
        self._dirty = True
        return {"ok": ok, "reason": ev.reason, "policy": pol if ok else None}

    async def pin(self, module: str | None) -> dict:
        """Закрепить фокус за модулем (или снять закрепление, module=None): другие модули не переключают фокус, кроме
        критических событий. Закрепление за модулем без фокуса сразу передаёт ему фокус."""
        async with self._lock:
            self.pinned = module or None
            self._dirty = True
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
            self._dirty = True
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
                "checkpoints": list(self.checkpoints.values()),
                "queue": [e.public() | {"priority": e.base, "dedupe_key": e.dedupe_key, "created": e.created}
                          for e in self.events.values() if e.status in (PENDING, DEFERRED)][:200]}

    async def _save(self) -> None:
        if self.state_path is None or not self._dirty:
            return
        text = json.dumps(self._state_dict(), ensure_ascii=False, default=str)
        try:
            await asyncio.to_thread(_write_json, self.state_path, text)
            self._dirty = False
        except OSError as e:                                    # checkpoint остаётся в памяти, запись повторится
            logger.warning(f"Фокус: состояние не записано на диск, повтор позже: {e}")

    def _load(self) -> None:
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"Фокус: файл состояния не прочитан: {e}")
            return
        if not isinstance(data, dict):
            return
        self.pinned = data.get("pin") if isinstance(data.get("pin"), str) else None
        self._dnd_local = bool(data.get("dnd_all"))
        self.dnd_modules = {m for m in data.get("dnd_modules") or [] if isinstance(m, str)}
        for cp in data.get("checkpoints") or []:
            if isinstance(cp, dict) and isinstance(cp.get("task_id"), str):
                self.checkpoints[cp["task_id"]] = cp
        now = self.clock()
        for raw in data.get("queue") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("module"), str):
                continue
            ev = QueuedEvent(id=str(raw.get("id") or _new_id("fe")), module=raw["module"], kind=str(raw.get("kind") or ""),
                             text=str(raw.get("text") or ""), priority=float(raw.get("priority") or 0.0),
                             base=float(raw.get("priority") or 0.0), created=float(raw.get("created") or now),
                             dedupe_key=raw.get("dedupe_key"), initial=float(raw.get("initial") or 0.0),
                             last_eval=now, updated=now, critical=bool(raw.get("critical")))
            ev.status = DEFERRED if raw.get("status") == DEFERRED else PENDING
            ev.resume = dict(raw.get("resume") or {})
            self.events[ev.id] = ev
        self._dirty = False

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
