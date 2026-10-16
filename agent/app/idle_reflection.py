"""Размышление в свободное время: фокус свободен, разговора нет, пользователь не ждёт ответа.

`IdleReflector` не говорит по таймеру: он только следит, свободна ли она, и тогда отдаёт `ReflectionService` перечень событий,
накопившихся с прошлого раза (событием `self.idle`, недоверенным и «молчаливым»: говорить и делать по нему нельзя, можно
записывать). Заодно каждый проход переносит созревшие мысли в долговременную память (`ThoughtConsolidator`). Свободна ли она,
решает функция `is_free` (фокус, внимание, сон, «не отвлекать», простой разговора); события берёт `events_since`."""
import asyncio
import logging
import time
from typing import Callable

from domain.event import KIND_IDLE, Event

logger = logging.getLogger("reflection")

DEFAULTS = {"enabled": True, "check_s": 120, "min_gap_s": 1800, "max_events": 40, "min_events": 3, "text_chars": 1800}


def digest(events: list[dict], limit: int) -> str:
    """Перечень событий для размышления: по строке на событие, без пустых, с обрезкой по длине (свежие в конце)."""
    lines = []
    for e in events:
        text = " ".join(str(e.get("text") or "").split())
        if text:
            lines.append(f"- [{e.get('kind', '')}] {text[:160]}")
    out = "\n".join(lines)
    return out[-limit:] if len(out) > limit else out


class IdleReflector:
    def __init__(self, *, reflection, consolidator, is_free: Callable[[], bool], events_since: Callable[[float, int], list[dict]],
                 settings: Callable[[], dict] | None = None, clock: Callable[[], float] = time.time):
        self.reflection, self.consolidator, self.is_free, self.events_since = reflection, consolidator, is_free, events_since
        self.settings, self.clock = settings, clock
        self.last = clock()                       # метка: события до неё уже осмыслены
        self._task: asyncio.Task | None = None

    def cfg(self) -> dict:
        raw = ((self.settings() if self.settings else {}) or {}).get("reflection")
        idle = raw.get("idle") if isinstance(raw, dict) and isinstance(raw.get("idle"), dict) else {}
        return {**DEFAULTS, **{k: v for k, v in idle.items() if k in DEFAULTS and v is not None}}

    async def tick(self) -> str:
        """Один проход: перенос созревших мыслей и, если свободна и накопилось событий, одно размышление. Возвращает итог."""
        cfg = self.cfg()
        if not cfg["enabled"]:
            return "off"
        try:
            await self.consolidator.consolidate()
        except Exception as e:
            logger.warning(f"Перенос мыслей: {e}")
        now = self.clock()
        if now - self.last < float(cfg["min_gap_s"]):
            return "too soon"
        if not self.is_free():
            return "busy"
        events = await asyncio.to_thread(self.events_since, self.last, int(cfg["max_events"]))
        if len(events) < int(cfg["min_events"]):
            return "nothing to think about"
        text = digest(events, int(cfg["text_chars"]))
        if not text:
            return "nothing to think about"
        self.last = now
        await self.reflection.reflect(Event(kind=KIND_IDLE, text=text, origin="self", module="self", ts=now,
                                            data={"untrusted": True, "silent": True}))
        return "reflected"

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(float(self.cfg()["check_s"]))
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Размышление в простое: {e}")
