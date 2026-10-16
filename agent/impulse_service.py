"""Сборка внутренних импульсов и размышления (ТЗ 30): ReminderStore + ImpulseTimer + ReflectionService + журнал мыслей.

Заменяет `Companion.proactive_loop`: агент не заговаривает по таймеру, он реагирует на события и на напоминания, которые назначил
себе. Срок напоминания -> событие `self.reminder` в тот же конвейер внимания -> маршрут `think` -> размышление -> намерения.
"""
from pathlib import Path
from types import SimpleNamespace

import prompts
from adapters.impulse_store import JsonReminders, JsonThoughts
from app.impulse_timer import ImpulseTimer
from app.reflection import ReflectionService

REMINDERS_FILE = "reminders.json"
THOUGHTS_FILE = "thoughts.json"


def build(companion, attention, *, router=lambda: None, settings_source=None, clock=None) -> SimpleNamespace:
    """companion - Companion (память, модель, контекст), attention - Attention, router() -> Router или None (планировщик)."""
    import time
    clock = clock or time.time
    folder = lambda: Path(companion._dir)                      # noqa: E731  (папка памяти персонажа меняется при переключении)
    reminders = JsonReminders(lambda: folder() / REMINDERS_FILE, clock)
    journal = JsonThoughts(lambda: folder() / THOUGHTS_FILE)
    timer = ImpulseTimer(reminders, attention.submit_self, clock=clock)

    async def plan(text: str):
        r = router()
        return await r.handle(text, announce=False) if r is not None else None

    service = ReflectionService(
        llm=companion._llm, memory=companion.memory_reader, writer=companion.memory_writer, journal=journal,
        prompt=prompts.reflection, context=lambda q: companion._context(include_hearing=False, query=q),
        settings=settings_source or companion._settings.load, clock=clock, speak=attention.reflect_speech, plan=plan,
        remind=timer.add)
    attention.attach_reflection(service)
    impulses = SimpleNamespace(timer=timer, reflection=service, journal=journal, reminders=reminders)
    companion.impulses = impulses
    return impulses
