"""Сборка внутренних импульсов и размышления (ТЗ 30): ReminderStore + ImpulseTimer + ReflectionService + журнал мыслей.

Заменил прежнюю проактивность по таймеру (`Companion.proactive_loop`, удалена): агент не заговаривает по таймеру, он реагирует на события и на напоминания, которые назначил
себе. Срок напоминания -> событие `self.reminder` в тот же конвейер внимания -> маршрут `think` -> размышление -> намерения.
"""
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import prompts
from adapters.impulse_store import JsonReminders, JsonThoughts
from app import impulse_origin
from app.impulse_timer import ImpulseTimer
from app.items import event_of
from app.plan_nudge import PlanNudger
from app.reflection import ReflectionService
from domain.event import KIND_AFTER_CONVERSATION

logger = logging.getLogger("impulses")

REMINDERS_FILE = "reminders.json"
THOUGHTS_FILE = "thoughts.json"


def build(companion, attention, *, settings_source=None, clock=None, tasks=None) -> SimpleNamespace:
    """companion - Companion (память, модель, контекст, единый агент), attention - Attention,
    tasks - TaskService (ТЗ 32): напоминания себе и планы пользователя живут в общих задачах; без него прежний `reminders.json`."""
    import time
    clock = clock or time.time
    folder = lambda: Path(companion._dir)                      # noqa: E731  (папка памяти персонажа меняется при переключении)
    reminders = tasks.reminders if tasks is not None and tasks.reminders is not None else JsonReminders(lambda: folder() / REMINDERS_FILE, clock)
    journal = JsonThoughts(lambda: folder() / THOUGHTS_FILE)
    timer = ImpulseTimer(reminders, attention.submit_self, clock=clock)

    async def plan(text: str):
        with impulse_origin.as_self():          # ТЗ 32: то, что агент сделает по этому намерению, это действие по самоимпульсу
            return await companion.resident.handle_event("self.action", text, "self")

    service = ReflectionService(
        llm=companion._llm, memory=companion.memory_reader, writer=companion.memory_writer, journal=journal,
        prompt=prompts.reflection, context=lambda q: companion._context(include_hearing=False, query=q),
        settings=settings_source or companion._settings.load, clock=clock, speak=attention.reflect_speech, plan=plan,
        remind=timer.add)
    attention.attach_reflection(service)

    def think(item):
        """Один агент: событие и самоимпульс обдумывает единый агент."""
        e = event_of(item)
        return attention.ctx.spawn(companion.resident.handle_event(e.kind, e.text, e.origin))
    attention.ctx.on_think = think

    def alert(item) -> bool:
        """Важное событие модуля (игра: враги, смерть, пожар) прерывает работу агента и разбирается им первым."""
        e = event_of(item)
        attention.ctx.spawn(companion.resident.preempt(e.kind, e.text, e.origin))
        return True
    attention.ctx.on_alert = alert
    plans = PlanNudger(tasks, timer.add, clock=clock)   # планы пользователя -> напоминания себе

    def conversation_ended(text: str) -> None:
        """IMP-10: разговор затих -> событие `self.after_conversation`; диспетчер отдаёт его в размышление (настроение, вывод)."""
        event = {"kind": KIND_AFTER_CONVERSATION, "text": text, "urgency_hint": 1, "data": {"untrusted": True}}
        task = asyncio.get_running_loop().create_task(attention.submit_self(event))
        task.add_done_callback(lambda t: t.cancelled() or t.exception() is None or logger.warning(f"Итог разговора: {t.exception()}"))

    companion.on_conversation_end = conversation_ended
    impulses = SimpleNamespace(timer=timer, reflection=service, journal=journal, reminders=reminders, plans=plans)
    companion.impulses = impulses
    return impulses
