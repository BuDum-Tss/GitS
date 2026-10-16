"""ImpulseTimer (ТЗ 30, IMP-3): следит только за сроками напоминаний, которые агент назначил себе.

Один таймер на ближайший срок. Срок наступил -> событие `self.reminder` (то же событие внимания, что и внешние). Не опрашивает
«стоит ли что-то сказать». При запуске восстанавливает сроки из хранилища: просроченное выдаётся одним событием с пометкой
«опоздало», а не пачкой; то, что пролежало дольше срока жизни, отменяется без события.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from contracts import ReminderRepository
from domain import impulses
from domain.event import KIND_REMINDER

logger = logging.getLogger("impulses")


class ImpulseTimer:
    def __init__(self, store: ReminderRepository, emit: Callable[[dict], Awaitable[None]], *,
                 clock: Callable[[], float] = time.time, late_s: float = impulses.LATE_S):
        """emit(event) принимает тело события (`Attention.submit("self", event)`)."""
        self.store, self.emit, self.clock, self.late_s = store, emit, clock, late_s
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Восстановить сроки и запустить ожидание ближайшего."""
        await self.fire_due()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def poke(self) -> None:
        """Список напоминаний изменился: пересчитать ближайший срок."""
        self._wake.set()

    async def add(self, due: float, text: str, **kw) -> dict:
        rec = await asyncio.to_thread(self.store.add, due, text, **kw)
        self.poke()
        return rec

    async def _run(self) -> None:
        while True:
            try:
                pending = await asyncio.to_thread(self.store.pending)
                self._wake.clear()
                wait = max(0.0, pending[0]["due"] - self.clock()) if pending else None
                if wait is None or wait > 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=wait)
                        continue                      # список изменился: пересчитать
                    except asyncio.TimeoutError:
                        pass
                await self.fire_due()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Таймер импульсов: {e}")
                await asyncio.sleep(5)

    async def fire_due(self) -> int:
        """Выдать события по наступившим срокам. Возвращает число выданных событий."""
        now = self.clock()
        pending = await asyncio.to_thread(self.store.pending)
        due: list[dict] = []
        for rec in pending:
            if rec["due"] > now:
                continue
            ttl = rec.get("ttl_s") or impulses.REMINDER_TTL_S
            if impulses.expired(rec["due"], now, ttl):
                await asyncio.to_thread(self.store.set_status, rec["id"], "cancelled")
                continue
            due.append(rec)
        if not due:
            return 0
        by_id = {r["id"]: r for r in due}
        on_time, late = impulses.split_due([(r["id"], r["due"]) for r in due], now, self.late_s)
        events = [self._event([by_id[i]], now, late=False) for i in on_time]
        if late:
            events.append(self._event([by_id[i] for i in late], now, late=True))
        for rec in due:
            await asyncio.to_thread(self.store.set_status, rec["id"], "fired")
        for ev in events:
            try:
                await self.emit(ev)
            except Exception as e:
                logger.warning(f"Напоминание себе не доставлено: {e}")
        return len(events)

    @staticmethod
    def _event(recs: list[dict], now: float, *, late: bool) -> dict:
        """Тело события: текст это факт для модели, не готовая реплика."""
        first = recs[0]
        lines = []
        for r in recs:
            mark = f" (опоздало на {int((now - r['due']) // 60)} мин)" if late else ""
            lines.append(f"{r['text']}{mark}")
        text = "Напоминание, которое ты назначила себе: " + "; ".join(lines)
        return {"kind": KIND_REMINDER, "text": text, "urgency_hint": 2,
                "data": {"reminder_ids": [r["id"] for r in recs], "late": late, "depth": max(int(r.get("depth", 0)) for r in recs),
                         "untrusted": any(r.get("untrusted") for r in recs), "source": first.get("source") or {}}}
