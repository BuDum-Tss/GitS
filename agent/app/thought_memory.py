"""Перенос мыслей из краткосрочной памяти в долговременную (после оценки важности).

Мысль живёт в журнале (`ThoughtJournal`) со стадией `short`. Правила переноса и забывания в `domain/thoughts.py`: важное и
выдержавшее `MIN_AGE_S` уходит в `MemoryWriter.add_fact` (вид `episode`, важность из оценки) и получает стадию `long`;
мелкое и устаревшее получает `dropped`. Модель для переноса не нужна: важность уже дала само размышление."""
import asyncio
import logging
import time
from typing import Callable

from contracts import MemoryWriter, ThoughtJournal
from domain import thoughts as dom

logger = logging.getLogger("reflection")


class ThoughtConsolidator:
    def __init__(self, *, journal: ThoughtJournal, writer: MemoryWriter, clock: Callable[[], float] = time.time, window: int = 200):
        self.journal, self.writer, self.clock, self.window = journal, writer, clock, window

    async def consolidate(self) -> dict:
        """Один проход по последним мыслям. Возвращает счётчики {promoted, dropped}."""
        items = await asyncio.to_thread(self.journal.recent, self.window)
        promote, drop = dom.plan(items, self.clock())
        done = {"promoted": 0, "dropped": 0}
        for t in promote:
            text = dom.memory_text(t)
            try:
                if text:
                    await asyncio.to_thread(self.writer.add_fact, text, "episode", dom.clean_importance(t.get("importance")))
                await asyncio.to_thread(self.journal.update, t["id"], stage=dom.LONG)
                done["promoted"] += 1
            except Exception as e:
                logger.warning(f"Перенос мысли в память: {e}")
        for t in drop:
            try:
                await asyncio.to_thread(self.journal.update, t["id"], stage=dom.DROPPED)
                done["dropped"] += 1
            except Exception as e:
                logger.warning(f"Забывание мысли: {e}")
        return done
