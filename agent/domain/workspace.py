"""Workspace: среды агента (ТЗ 32, WS-1). Чистые правила без ввода-вывода, настроек и часов.

Агент в каждый момент находится ровно в одной среде. `assistant` и `sleep` встроены в ядро, `rimworld` и `montage` это среды
модулей. В среду модуля входят только по импульсу (сообщение пользователя, самоимпульс агента) или по критическому сигналу;
запуск процесса, окно и состояние модуля среду не включают. Сон начинает планировщик сна (простой, расписание).
"""
from dataclasses import dataclass
from fnmatch import fnmatchcase

ASSISTANT = "assistant"        # базовая среда: наблюдение за ПК, разговор, память
SLEEP = "sleep"                # агент сам не активен, идёт обработка воспоминаний (ТЗ 27)

KIND_BUILTIN = "builtin"
KIND_MODULE = "module"


class Reason:
    """Причина смены среды (ТЗ 32, раздел 4.3)."""
    USER = "user"                  # сообщение или команда пользователя
    SELF = "self"                  # импульс агента (ТЗ 30)
    SCHEDULE = "schedule"          # планировщик сна: простой, расписание
    DONE = "done"                  # задача среды закончилась
    BLOCKED = "blocked"            # модуль сообщил, что продолжить не может
    CRITICAL = "critical"          # критический сигнал: решают правила, среда вправе отказать в небезопасной точке
    DISCONNECT = "disconnect"      # модуль отключился
    START = "start"                # старт ядра

    ALL = (USER, SELF, SCHEDULE, DONE, BLOCKED, CRITICAL, DISCONNECT, START)
    FORCED = (DONE, BLOCKED, DISCONNECT, USER)     # выход в эти моменты не ждёт безопасной точки


CRITICAL_URGENCY = 5               # срочность критического сигнала (ТЗ 32, WS-6)

# значения `entry` в манифесте модуля (ТЗ 32, 4.1) -> причины входа; запуск процесса и окно сюда не попадают
ENTRY_REASONS = {"user_message": Reason.USER, "self_impulse": Reason.SELF, "schedule": Reason.SCHEDULE,
                 "critical": Reason.CRITICAL}


@dataclass(frozen=True)
class WorkspaceSpec:
    id: str
    title: str
    kind: str = KIND_MODULE
    purpose: str = ""
    entry: tuple[str, ...] = ()        # причины, по которым в среду можно войти
    interruptible: bool = True         # можно ли выйти, когда среда не сообщает иного
    safe_points: tuple[str, ...] = ()  # когда выход безопасен (для промпта и дашборда)
    signals_in: tuple[str, ...] = ()   # какие чужие сигналы среда принимает уведомлениями (шаблоны видов); пусто - все
    done_when: str = ""                # когда задача среды считается законченной


BUILTIN: dict[str, WorkspaceSpec] = {
    ASSISTANT: WorkspaceSpec(ASSISTANT, "Ассистент", KIND_BUILTIN, "наблюдение за ПК, разговор, память",
                             entry=tuple(Reason.ALL)),
    SLEEP: WorkspaceSpec(SLEEP, "Сон", KIND_BUILTIN, "обработка воспоминаний, сама не активна", entry=(Reason.SCHEDULE,)),
}

MODULE_ENTRY = (Reason.USER, Reason.SELF, Reason.CRITICAL)


def module_spec(module_id: str, title: str = "", purpose: str = "") -> WorkspaceSpec:
    return WorkspaceSpec(module_id, title or module_id, KIND_MODULE, purpose, entry=MODULE_ENTRY)


def spec_from_manifest(module_id: str, ws: dict, title: str = "") -> WorkspaceSpec:
    """Среда модуля из поля `workspace` манифеста (ТЗ 32, WS-4; поля проверил `validate_manifest`). Причины входа берутся из
    `entry`; автоматического `process_started` в них нет и быть не может."""
    names = ws.get("entry") or ("user_message", "self_impulse", "critical")
    entry = tuple(dict.fromkeys(ENTRY_REASONS[n] for n in names if n in ENTRY_REASONS))
    return WorkspaceSpec(module_id, str(ws.get("title") or title or module_id), KIND_MODULE, str(ws.get("purpose") or ""),
                         entry=entry, interruptible=bool(ws.get("interruptible", True)),
                         safe_points=tuple(ws.get("safe_points") or ()), signals_in=tuple(ws.get("signals_in") or ()),
                         done_when=str(ws.get("done_when") or ""))


def can_enter(spec: WorkspaceSpec, reason: str) -> tuple[bool, str]:
    """(можно, причина отказа). Запуск игры или окно не входят в список причин."""
    if reason not in Reason.ALL:
        return False, f"неизвестная причина: {reason}"
    if reason not in spec.entry:
        return False, f"в среду «{spec.title}» нельзя войти по причине «{reason}»"
    return True, ""


def can_exit(spec: WorkspaceSpec, interruptible_now: bool, reason: str) -> tuple[bool, str]:
    """(можно, причина отказа). Конец задачи, блокировка, отключение и просьба пользователя не ждут безопасной точки;
    самоимпульс, расписание и критический сигнал ждут её."""
    if reason in Reason.FORCED or spec.kind == KIND_BUILTIN:
        return True, ""
    if spec.interruptible and interruptible_now:
        return True, ""
    return False, f"среда «{spec.title}» сейчас небезопасна для выхода"


def exit_target(reason: str) -> str:
    """Куда уходим, когда среда закончилась: всегда в базовую."""
    return ASSISTANT


def switch_record(ts: float, old: str, new: str, reason: str, goal: str = "", note: str = "") -> dict:
    return {"ts": ts, "from": old, "to": new, "reason": reason, "goal": goal, "note": note}


# ── сигналы (WS-6): события чужих сред не борются за слот ──────────────────────────

SIGNAL_OWN = "own"             # событие текущей среды: она обрабатывает его сама
SIGNAL_NOTIFY = "notify"       # уведомление: среда принимает такие сигналы (signals_in)
SIGNAL_DIGEST = "digest"       # копится в сводке
SIGNAL_SWITCH = "switch"       # критический: переключает среду по правилам без LLM (среда вправе отказать в небезопасной точке)


def accepts_signal(spec: WorkspaceSpec, kind: str) -> bool:
    """Принимает ли среда сигнал такого вида уведомлением. Встроенная `assistant` принимает всё, `sleep` ничего (она молчит);
    у модульной среды пустой `signals_in` значит «всё»."""
    if spec.id == SLEEP:
        return False
    if not spec.signals_in:
        return True
    return any(fnmatchcase(kind, pat) for pat in spec.signals_in)


def route_signal(current: WorkspaceSpec, target: WorkspaceSpec | None, kind: str, module: str, urgency: int | None) -> str:
    """Что делать с событием модуля `module` в текущей среде `current`. `target` это среда этого модуля, если она есть."""
    if module == current.id:
        return SIGNAL_OWN
    if (urgency or 0) >= CRITICAL_URGENCY and target is not None and Reason.CRITICAL in target.entry:
        return SIGNAL_SWITCH
    return SIGNAL_NOTIFY if accepts_signal(current, kind) else SIGNAL_DIGEST
