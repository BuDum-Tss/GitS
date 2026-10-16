"""Очередь событий менеджера фокуса (ТЗ 29, ARC-6; ТЗ 18, FOC-3): состояния событий, склейка одинаковых, сроки,
старение приоритета, возврат отложенных на оценку, чистка. Часов и сети нет: время приходит аргументом, журнал и
пометка «состояние изменилось» приходят колбэками."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Callable

from domain import focus_policy as policy
from domain.focus_policy import ECHELON_RULES

CRITICAL_URGENCY = policy.CRITICAL_URGENCY
RECHECK_S = 30.0               # отложенные события переоцениваются раз в RECHECK_S (и при смене контекста)
EVENT_TTL_S = 1800.0           # отложенное событие, которое так и не стало важным, истекает через полчаса
DEDUPE_S = 60.0                # одинаковые события одного модуля в это окно склеиваются
RESOLVED_KEEP_S = 600.0

PENDING = "pending"            # ждёт оценки
DEFERRED = "deferred"          # отложено, ждёт условия возобновления
PROCESSING = "processing"      # идёт переход фокуса
RESOLVED = "resolved"          # обработано (фокус передан или модуль сам справится)
EXPIRED = "expired"            # срок вышел
DISCARDED = "discarded"        # слито с другим событием

EVENT_ALIVE = (PENDING, DEFERRED, PROCESSING)

Log = Callable[..., None]


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


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


def decision_fields(ev: QueuedEvent) -> dict:
    """Эшелон и уверенность для записи решения (ТЗ 18, FOC-8, аудит)."""
    return {"echelon": ev.echelon, "confidence": ev.confidence, "hint": ev.hint}


def event_echelon(ev: QueuedEvent) -> str:
    """Какой эшелон решил: Gemma, иначе GLiNER (если давал ответ), иначе правила."""
    return policy.echelon(ev.llm_used, ev.critical,
                          ev.confidence is not None or ev.hint is not None or ev.focus_needed is not None)


class FocusQueue:
    def __init__(self, *, log: Log, touch: Callable[[], None]):
        """log(тип, **поля) - аудит; touch() - отметить, что состояние нужно записать на диск."""
        self.events: dict[str, QueuedEvent] = {}
        self.inbox: list[dict] = []
        self.once: set[str] = set()               # модули с разовым исключением из «не беспокоить»
        self.modules_seen: set[str] = set()
        self._log = log
        self._touch = touch

    def push(self, rec: dict) -> None:
        self.inbox.append(rec)

    def add(self, ev: QueuedEvent) -> None:
        self.events[ev.id] = ev

    def drain(self, now: float) -> None:
        """ATT-6 и FOC-3: одинаковое событие в окне DEDUPE_S склеивается с ожидающим, остальные становятся очередью."""
        batch, self.inbox = self.inbox, []
        for rec in batch:
            module = rec["module"]
            self.modules_seen.add(module)
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
            ev = QueuedEvent(id=new_id("fe"), module=module, kind=rec["kind"], text=rec["text"], priority=rec["priority"],
                             base=rec["priority"], created=now, urgency=rec["urgency"], critical=rec["critical"],
                             confidence=rec["confidence"], category=rec["category"], expires=rec["expires"],
                             dedupe_key=rec["dedupe_key"], initial=rec["priority"], last_eval=now, updated=now,
                             hint=policy.hint_value(rec.get("hint")), hint_conf=float(rec.get("hint_conf") or 0.0),
                             focus_needed=rec.get("focus_needed") if isinstance(rec.get("focus_needed"), bool) else None,
                             focus_conf=float(rec.get("focus_conf") or 0.0))
            if not ev.critical and module in self.once:
                self.once.discard(module)
                ev.once = True
            ev.echelon = event_echelon(ev)
            self.events[ev.id] = ev
            self._touch()
            self._log("enqueue", event=ev.id, module=module, kind=rec["kind"], priority_before=round(rec["priority"], 1),
                      critical=ev.critical, status=PENDING, text=ev.text, **decision_fields(ev))

    def expire(self, now: float) -> None:
        for ev in self.events.values():
            if ev.status not in (PENDING, DEFERRED):
                continue
            if ev.expires is not None and now > ev.expires:
                self.finish(ev, EXPIRED, "срок события вышел", now)
            elif ev.status == DEFERRED and now - ev.created > EVENT_TTL_S:
                self.finish(ev, EXPIRED, "отложено дольше срока ожидания", now)

    def age(self, now: float) -> None:
        """FOC-3, защита от вечного откладывания: ожидание поднимает приоритет до AGING_CAP."""
        for ev in self.events.values():
            if ev.status not in (PENDING, DEFERRED) or ev.critical:
                continue
            aged = policy.aged_priority(ev.base, now - ev.created)
            if aged > ev.priority:
                ev.priority = aged
                self._log("aging", event=ev.id, module=ev.module, priority_before=round(ev.base, 1),
                          priority_after=round(aged, 1), reason="ожидание поднимает приоритет")

    def finish(self, ev: QueuedEvent, status: str, reason: str, now: float) -> None:
        ev.status, ev.reason, ev.updated = status, reason, now
        ev.resume = {}
        self._touch()
        self._log("resolve" if status == RESOLVED else status, event=ev.id, module=ev.module, kind=ev.kind,
                  priority_before=round(ev.initial, 1), priority_after=round(ev.priority, 1), reason=reason,
                  status=status, **decision_fields(ev))

    def defer(self, ev: QueuedEvent, resume: dict, reason: str, now: float) -> None:
        """FOC-3: отложить с причиной. Повтор той же причины не пишет журнал заново."""
        ev.last_eval = now
        if ev.status == DEFERRED and ev.reason == reason:
            return
        ev.status, ev.reason, ev.resume, ev.updated = DEFERRED, reason, dict(resume), now
        self._touch()
        self._log("defer", event=ev.id, module=ev.module, kind=ev.kind, priority_before=round(ev.initial, 1),
                  priority_after=round(ev.priority, 1), reason=reason, resume=resume, status=DEFERRED,
                  **decision_fields(ev))

    def refresh(self, now: float, changed: bool) -> None:
        """FOC-3: отложенные события возвращаются на оценку при смене контекста и раз в RECHECK_S; «время» возвращает,
        когда наступило."""
        for ev in self.events.values():
            if ev.status != DEFERRED:
                continue
            if ev.resume.get("type") == "time":
                if now >= float(ev.resume.get("at") or 0):
                    ev.status = PENDING
            elif changed or now - ev.last_eval >= RECHECK_S:
                ev.status = PENDING

    def prune(self, now: float) -> None:
        for eid in [e.id for e in self.events.values() if e.status not in EVENT_ALIVE
                    and now - e.updated > RESOLVED_KEEP_S]:
            del self.events[eid]

    def pending_sorted(self) -> list[QueuedEvent]:
        return sorted((e for e in self.events.values() if e.status == PENDING),
                      key=lambda e: (not e.preferred, -e.priority, e.created))

    def waiting(self) -> list[QueuedEvent]:
        return [e for e in self.events.values() if e.status in (PENDING, DEFERRED)]

    def dump(self) -> list[dict]:
        return [e.public() | {"priority": e.base, "dedupe_key": e.dedupe_key, "created": e.created}
                for e in self.events.values() if e.status in (PENDING, DEFERRED)][:200]

    def load(self, raws: Any, now: float) -> None:
        for raw in raws or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("module"), str):
                continue
            ev = QueuedEvent(id=str(raw.get("id") or new_id("fe")), module=raw["module"], kind=str(raw.get("kind") or ""),
                             text=str(raw.get("text") or ""), priority=float(raw.get("priority") or 0.0),
                             base=float(raw.get("priority") or 0.0), created=float(raw.get("created") or now),
                             dedupe_key=raw.get("dedupe_key"), initial=float(raw.get("initial") or 0.0),
                             last_eval=now, updated=now, critical=bool(raw.get("critical")))
            ev.status = DEFERRED if raw.get("status") == DEFERRED else PENDING
            ev.resume = dict(raw.get("resume") or {})
            self.events[ev.id] = ev
