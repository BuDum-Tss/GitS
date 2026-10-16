"""Workspace: среды агента (ТЗ 32, WS-1). Чистые правила без ввода-вывода, настроек и часов.

Агент в каждый момент находится ровно в одной среде. `assistant` и `sleep` встроены в ядро, `rimworld` и `montage` это среды
модулей. В среду модуля входят только по импульсу (сообщение пользователя, самоимпульс агента) или по критическому сигналу;
запуск процесса, окно и состояние модуля среду не включают. Сон начинает планировщик сна (простой, расписание).
"""
from dataclasses import dataclass

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


@dataclass(frozen=True)
class WorkspaceSpec:
    id: str
    title: str
    kind: str = KIND_MODULE
    purpose: str = ""
    entry: tuple[str, ...] = ()        # причины, по которым в среду можно войти
    interruptible: bool = True         # можно ли выйти, когда среда не сообщает иного


BUILTIN: dict[str, WorkspaceSpec] = {
    ASSISTANT: WorkspaceSpec(ASSISTANT, "Ассистент", KIND_BUILTIN, "наблюдение за ПК, разговор, память",
                             entry=tuple(Reason.ALL)),
    SLEEP: WorkspaceSpec(SLEEP, "Сон", KIND_BUILTIN, "обработка воспоминаний, сама не активна", entry=(Reason.SCHEDULE,)),
}

MODULE_ENTRY = (Reason.USER, Reason.SELF, Reason.CRITICAL)

# среды модулей, известные ядру (id модуля -> название); потом их объявляет манифест (WS-4)
MODULE_WORKSPACES: dict[str, str] = {"rimworld": "RimWorld", "montage": "Монтаж"}


def module_spec(module_id: str, title: str = "", purpose: str = "") -> WorkspaceSpec:
    return WorkspaceSpec(module_id, title or module_id, KIND_MODULE, purpose, entry=MODULE_ENTRY)


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
