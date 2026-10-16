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
from app.idle_reflection import IdleReflector
from app.impulse_timer import ImpulseTimer
from app.plan_nudge import PlanNudger
from app.reflection import ReflectionService
from app.thought_memory import ThoughtConsolidator
from domain.event import KIND_AFTER_CONVERSATION
from memory_store import effective_due

logger = logging.getLogger("impulses")

REMINDERS_FILE = "reminders.json"
THOUGHTS_FILE = "thoughts.json"


def build(companion, attention, *, router=lambda: None, settings_source=None, clock=None, tasks=None) -> SimpleNamespace:
    """companion - Companion (память, модель, контекст), attention - Attention, router() -> Router или None (планировщик),
    tasks - TaskService (ТЗ 32): напоминания себе и планы пользователя живут в общих задачах; без него прежний `reminders.json`."""
    import time
    clock = clock or time.time
    folder = lambda: Path(companion._dir)                      # noqa: E731  (папка памяти персонажа меняется при переключении)
    reminders = tasks.reminders if tasks is not None and tasks.reminders is not None else JsonReminders(lambda: folder() / REMINDERS_FILE, clock)
    journal = JsonThoughts(lambda: folder() / THOUGHTS_FILE)
    timer = ImpulseTimer(reminders, attention.submit_self, clock=clock)

    async def plan(text: str):
        r = router()
        with impulse_origin.as_self():          # ТЗ 32: то, что планировщик сделает по этому намерению, это действие по самоимпульсу
            return await r.handle(text, announce=False) if r is not None else None

    service = ReflectionService(
        llm=companion._llm, memory=companion.memory_reader, writer=companion.memory_writer, journal=journal,
        prompt=prompts.reflection, context=lambda q: companion._context(include_hearing=False, query=q),
        settings=settings_source or companion._settings.load, clock=clock, speak=attention.reflect_speech, plan=plan,
        remind=timer.add)
    attention.attach_reflection(service)
    plans = PlanNudger(companion.memory_reader, timer.add, due_of=effective_due, clock=clock, tasks=tasks)   # планы пользователя -> напоминания себе

    def conversation_ended(text: str) -> None:
        """IMP-10: разговор затих -> событие `self.after_conversation`; диспетчер отдаёт его в размышление (настроение, вывод)."""
        event = {"kind": KIND_AFTER_CONVERSATION, "text": text, "urgency_hint": 1, "data": {"untrusted": True}}
        task = asyncio.get_running_loop().create_task(attention.submit_self(event))
        task.add_done_callback(lambda t: t.cancelled() or t.exception() is None or logger.warning(f"Итог разговора: {t.exception()}"))

    companion.on_conversation_end = conversation_ended
    consolidator = ThoughtConsolidator(journal=journal, writer=companion.memory_writer, clock=clock)
    impulses = SimpleNamespace(timer=timer, reflection=service, journal=journal, reminders=reminders, plans=plans,
                               consolidator=consolidator, idle=None)
    companion.impulses = impulses
    return impulses


def attach_idle(impulses, companion, *, is_free, events_since=None, settings_source=None, clock=None) -> IdleReflector:
    """Размышление в свободное время: `is_free()` - свободна ли она (фокус, внимание, сон, «не отвлекать»); события для осмысления
    берутся из журнала событий, если `events_since` не задан. Запуск: `impulses.idle.start()`."""
    import time

    import events_log
    clock = clock or time.time
    kinds = {"user", "say", "telegram", "game", "module_event", "vision", "hearing", "plan"}
    events_since = events_since or (lambda since, limit: events_log.tail(limit, kinds, since))
    idle = IdleReflector(reflection=impulses.reflection, consolidator=impulses.consolidator, is_free=is_free,
                         events_since=events_since, settings=settings_source or companion._settings.load, clock=clock)
    impulses.idle = idle
    return idle
