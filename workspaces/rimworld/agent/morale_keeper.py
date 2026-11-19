"""Настроение без модели: страховка от массовых срывов, пока агент развивает колонию (настройка модуля `morale_keeper`, по умолчанию включена).

`plan_morale` чистая: раз в `INTERVAL_S` секунд в спокойное время (нет угроз) по состоянию игры решает три вещи, применяет controller
(команды мода `set_schedule` с признаком `reflex: true`: расписание правится без открытого окна):
  а) расписание: у колониста с «шаблонным» расписанием (всё Работа или Anything, сон только в часы по умолчанию) появляются
     часы сна (Sleep 22-5) и отдыха (Joy 20-21). Расписание, которое настроил агент, не трогаем: это любое расписание с Joy, с Meditate
     или со сном в других часах; и всё, пока агент сам вызывал инструменты расписания меньше `AGENT_SCHEDULE_QUIET_S` секунд назад;
  б) колонисту с настроением ниже `LOW_MOOD` на ближайшие `JOY_HOURS` часа ставится Joy (до того были Работа или Anything), потом
     прежнее расписание возвращается. Приоритеты работ не трогаем: Joy-часы и так снимают с работы, а приоритеты настроил агент;
  в) предупреждение `rimworld.morale_warning` ядру (факт, не приказ), когда у трети колонистов и больше настроение ниже `WARN_MOOD`:
     самые частые причины из `mood_thoughts`.
"""
from dataclasses import dataclass, field

INTERVAL_S = 30.0
LOW_MOOD = 0.35                  # ниже: колонисту срочно нужен отдых
WARN_MOOD = 0.4                  # ниже: колонист считается в группе риска для предупреждения
WARN_SHARE = 1 / 3               # доля колонистов в группе риска, с которой предупреждаем
WARN_REPEAT_S = 150.0            # предупреждение повторяем не чаще (ядро заменяет прежнее)
AGENT_SCHEDULE_QUIET_S = 600.0   # агент правил расписание недавно: не переписываем
TICKS_PER_HOUR = 2500
JOY_HOURS = 3                    # сколько часов подряд отдыхает колонист с низким настроением
JOY_COOLDOWN_TICKS = 5 * TICKS_PER_HOUR
DEFAULT_SLEEP = frozenset(list(range(0, 6)) + [22, 23])      # сон по умолчанию в игре: 22-5
SLEEP_HOURS = (22, 23, 0, 1, 2, 3, 4, 5)
JOY_SCHEDULE_HOURS = (20, 21)
MAX_ATTEMPTS = 3                 # сколько раз пробуем выставить расписание, если игра его не принимает
RETRY_S = 60.0


@dataclass
class Memory:
    last_run: float = 0.0
    attempts: dict = field(default_factory=dict)             # id колониста -> (сколько раз пытались, когда в последний раз)
    temp: dict = field(default_factory=dict)                 # id -> {"until": тик, "orig": [(час, было)]}: временный Joy
    cooldown_until: dict = field(default_factory=dict)       # id -> тик, до которого новый временный Joy не ставим
    warned_at: float = -1e9

    def reset(self) -> None:
        self.__init__()


@dataclass
class MoralePlan:
    actions: list = field(default_factory=list)              # команды мода (dict, уже с reflex)
    warning: dict | None = None                              # {"text", "data", "urgency"}
    notes: list = field(default_factory=list)                # короткие строки для протокола


def _colonists(state: dict) -> list[dict]:
    return [p for p in (state.get("colony") or {}).get("pawns") or [] if p.get("id") and not p.get("is_prisoner")]


def is_template_schedule(schedule: list) -> bool:
    """Расписание как у новой игры или ещё проще: всё Работа или Anything, сон только в часах по умолчанию (22-5). Любое Joy,
    Meditate или сон в других часах значит, что расписание настраивали руками."""
    if not isinstance(schedule, list) or len(schedule) != 24:
        return False
    for hour, kind in enumerate(schedule):
        if kind in ("Work", "Anything"):
            continue
        if kind == "Sleep" and hour in DEFAULT_SLEEP:
            continue
        return False
    return True


def _set(pid: str, hour: int, kind: str) -> dict:
    return {"type": "set_schedule", "pawn_id": pid, "hour": hour, "schedule_type": kind, "reflex": True}


def mood_causes(pawns: list[dict], limit: int = 3) -> list[tuple[str, int, float]]:
    """Самые частые отрицательные мысли среди колонистов: (мысль, у скольких, суммарный эффект), по числу и силе."""
    count: dict[str, int] = {}
    total: dict[str, float] = {}
    for p in pawns:
        seen = set()
        for t in p.get("mood_thoughts") or []:
            label, eff = t.get("label"), t.get("effect")
            if not label or not isinstance(eff, (int, float)) or eff >= 0 or label in seen:
                continue
            seen.add(label)
            count[label] = count.get(label, 0) + 1
            total[label] = total.get(label, 0.0) + eff
    ranked = sorted(count, key=lambda k: (-count[k], total[k], k))
    return [(k, count[k], round(total[k], 1)) for k in ranked[:limit]]


def plan_morale(state: dict, now: float, mem: Memory, *, agent_schedule_at: float | None = None, enabled: bool = True,
                interval_s: float = INTERVAL_S) -> MoralePlan:
    """Что сделать сейчас. Вызывать только в спокойное время; раз в `interval_s` секунд (раньше пустой план)."""
    plan = MoralePlan()
    if not enabled:
        mem.reset()
        return plan
    if now - mem.last_run < interval_s:
        return plan
    colonists = _colonists(state)
    if not colonists:
        return plan
    mem.last_run = now
    tick = state.get("tick") if isinstance(state.get("tick"), int) else None
    hour = state.get("hour") if isinstance(state.get("hour"), int) else None
    agent_busy = agent_schedule_at is not None and now - agent_schedule_at < AGENT_SCHEDULE_QUIET_S
    names = {p["id"]: p.get("name") or p["id"] for p in colonists}

    # временный Joy кончился: прежнее расписание обратно
    for pid, rec in list(mem.temp.items()):
        pawn = next((p for p in colonists if p["id"] == pid), None)
        if pawn is None:
            del mem.temp[pid]
        elif tick is not None and tick >= rec["until"]:
            if not agent_busy:
                plan.actions += [_set(pid, h, orig) for h, orig in rec["orig"] if (pawn.get("schedule") or [None] * 24)[h] == "Joy"]
            del mem.temp[pid]
            plan.notes.append(f"{names[pid]}: отдых окончен, расписание возвращено")

    # (а) расписание
    if not agent_busy:
        for p in colonists:
            pid, sched = p["id"], p.get("schedule")
            if not is_template_schedule(sched) or pid in mem.temp:
                if sched and pid not in mem.temp:
                    mem.attempts.pop(pid, None)             # расписание уже не шаблонное (наше или агента): счёт попыток не нужен
                continue
            tries, last = mem.attempts.get(pid, (0, -1e9))
            if tries >= MAX_ATTEMPTS or now - last < RETRY_S:
                continue
            todo = [(h, "Sleep") for h in SLEEP_HOURS if sched[h] != "Sleep"] + [(h, "Joy") for h in JOY_SCHEDULE_HOURS]
            mem.attempts[pid] = (tries + 1, now)
            plan.actions += [_set(pid, h, kind) for h, kind in todo]
            plan.notes.append(f"{names[pid]}: расписание шаблонное, ставлю сон 22-5 и отдых 20-21")

    # (б) низкое настроение: ближайшие часы под отдых
    if hour is not None and tick is not None and not agent_busy:
        for p in colonists:
            pid, mood = p["id"], p.get("mood_pct")
            if (not isinstance(mood, (int, float)) or mood >= LOW_MOOD or p.get("is_downed") or p.get("is_drafted")
                    or p.get("mental_state") or p.get("in_mental_break") or pid in mem.temp
                    or tick < mem.cooldown_until.get(pid, 0)):
                continue
            sched = p.get("schedule") or []
            if len(sched) != 24:
                continue
            hours = [(hour + i) % 24 for i in range(JOY_HOURS)]
            orig = [(h, sched[h]) for h in hours if sched[h] in ("Work", "Anything")]
            if not orig:
                continue
            mem.temp[pid] = {"until": tick + JOY_HOURS * TICKS_PER_HOUR, "orig": orig}
            mem.cooldown_until[pid] = tick + JOY_HOURS * TICKS_PER_HOUR + JOY_COOLDOWN_TICKS
            plan.actions += [_set(pid, h, "Joy") for h, _ in orig]
            plan.notes.append(f"{names[pid]}: настроение {round(mood * 100)}%, отдых на {len(orig)} ч (Joy с {orig[0][0]:02d}:00)")

    # (в) предупреждение ядру: факт
    low = [p for p in colonists if isinstance(p.get("mood_pct"), (int, float)) and p["mood_pct"] < WARN_MOOD]
    if low and len(low) >= len(colonists) * WARN_SHARE - 1e-9 and now - mem.warned_at >= WARN_REPEAT_S:
        mem.warned_at = now
        causes = mood_causes(low)
        why = "; ".join(f"{label} (у {n})" for label, n, _ in causes) or "причины в мыслях не видны"
        text = f"У {len(low)} из {len(colonists)} колонистов настроение ниже 0,4: {why}."
        plan.warning = {"text": text, "urgency": 3, "data": {
            "low": [names[p["id"]] for p in low], "total": len(colonists),
            "causes": [{"thought": k, "pawns": n, "effect": e} for k, n, e in causes]}}
    return plan
