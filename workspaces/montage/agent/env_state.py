"""Окружение «Монтаж» (ТЗ 32, WS-4; docs/WORKSPACE_CONTRACT.md раздел 7): в фокусе ли оно у агента. Чистое состояние без ввода-вывода.

Ядро шлёт `workspace.enter` (занять и начать работу), `workspace.afk` (фокус снят, окружение остаётся за ней, операции идут),
`workspace.exit` (выход с отпусканием) и после перезапуска ядра `workspace.resume`. Окружение само не кончается: когда операции
закончились, она продолжает работать в нём, пока не выйдет (exit) или не отойдёт (afk). Статус для ядра (`state.data.status`):
`done` (окружение не в фокусе) и можно ли выйти сейчас.
"""
from __future__ import annotations


class WorkspaceState:
    def __init__(self) -> None:
        self.active = False            # окружение в фокусе у агента
        self.afk = False               # агент отошёл, не отпуская
        self.releasing = False         # агент вышла (exit): новые операции начинать нельзя, идут оставшиеся
        self.goal = ""
        self.task_id = ""

    def enter(self, goal: str = "", task_id: str = "", restored: bool = False) -> dict:
        """Вход (и возврат из AFK, и повтор после перезапуска ядра): отпускание отменяется."""
        self.active, self.afk, self.releasing = True, False, False
        if goal or not self.goal:
            self.goal = goal
        self.task_id = task_id or self.task_id
        return {"ok": True, "restored": restored}

    def go_afk(self) -> dict:
        """Отошла, не отпуская: фокус снят, всё остальное на месте."""
        was = self.active
        self.active, self.afk = False, True
        return {"ok": True, "checkpoint": {"goal": self.goal, "was_active": was, "afk": True}}

    def exit(self, checkpoint: dict | None = None) -> dict:
        was = self.active
        self.active, self.afk, self.releasing = False, False, True
        return {"ok": True, "checkpoint": checkpoint or {"goal": self.goal, "was_active": was}}

    def status(self) -> dict:
        return {"done": not self.active, "interruptible": True}
