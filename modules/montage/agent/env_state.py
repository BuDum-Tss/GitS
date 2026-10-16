"""Среда агента «Монтаж» (ТЗ 32, WS-4): входит ли агент в монтаж сейчас. Чистое состояние без ввода-вывода.

Ядро вводит агента в среду запросом `workspace.enter` (прежнее `policy.activate`), только по сообщению пользователя, по
самоимпульсу или по критическому сигналу. Рендер идёт и без среды: фоновая задача модуля фокуса не требует. Статус для ядра
(`state.data.status`): активная политика (`working` или `idle`), можно ли выйти сейчас, нет ли блокировки. Среда кончается
сама (`idle`), когда задачи, начатые при ней, закончились: ядро видит возврат в фон и возвращает агента в assistant.
"""
from __future__ import annotations

ACTIVE, IDLE = "working", "idle"
# стадии, в которых выходить небезопасно: идёт сборка или рендер, пользователь ждёт результат
UNSAFE_STAGES = ("build", "final")


class WorkspaceState:
    def __init__(self) -> None:
        self.active = False
        self.had_work = False          # при среде была поставлена или шла задача: когда она кончится, среда закончена
        self.goal = ""
        self.task_id = ""

    def enter(self, goal: str = "", task_id: str = "", busy: bool = False, restored: bool = False) -> dict:
        self.active, self.goal, self.task_id = True, goal, task_id
        self.had_work = busy
        return {"ok": True, "policy": ACTIVE, "restored": restored}

    def exit(self, checkpoint: dict | None = None) -> dict:
        was = self.active
        self.active = False
        self.had_work = False
        return {"ok": True, "checkpoint": checkpoint or {"goal": self.goal, "was_active": was}, "policy": IDLE}

    def note_job(self) -> None:
        """Задача поставлена или идёт, пока агент в среде."""
        if self.active:
            self.had_work = True

    def finish_if_done(self, busy: bool) -> bool:
        """Все задачи среды кончились: среда закончена (done). True, если состояние изменилось."""
        if self.active and self.had_work and not busy:
            self.active, self.had_work = False, False
            return True
        return False

    def status(self, stage: str | None = None) -> dict:
        return {"active_policy": ACTIVE if self.active else IDLE,
                "interruptible": not (self.active and stage in UNSAFE_STAGES)}
