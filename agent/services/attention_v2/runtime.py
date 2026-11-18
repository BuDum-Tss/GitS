"""Контракт Runtime для внимания (RUNTIME_SPEC 5.3) и запись действия хода (5.1)."""
from dataclasses import dataclass
from typing import Protocol

from .event import Event


@dataclass
class ActionRecord:
    """Строка журнала действий хода: вызванный инструмент, статус и признак побочного эффекта (`say`, `text`, действия в играх)."""
    tool: str
    status: str = "started"               # started | done | failed
    effect: bool = False
    summary: str = ""                     # краткий результат («поставлено», что прозвучало)
    started_at: float | None = None


class Runtime(Protocol):
    """То, что вниманию нужно от запуска ходов агента. Конструктор внимания знает Runtime только через этот протокол."""

    can_steer: bool

    def current_batch(self) -> list[Event]:
        """Пачка идущего хода; пусто, если агент простаивает."""

    async def trigger(self, events: list[Event], after_ms: int = 0) -> None:
        """Запустить ход с пачкой. Если ход идёт, это ошибка вызова."""

    async def interrupt(self, new_events: list[Event]) -> None:
        """Прервать ход и запустить новый с `new_events` (раздел 5.2). Прерванные события Runtime возвращает вниманию через
        `Attention.requeue_interrupted(events, had_effects)` и передаёт блок `Attention.build_interrupt_note(...)`."""

    async def steer(self, event: Event) -> bool:
        """Подлить событие в идущий ход; False, если не вышло (тогда событие встанет в очередь)."""

    async def trigger_idle(self) -> None:
        """Ход без событий: проактивная мысль, сон, рефлексия (4.4)."""
