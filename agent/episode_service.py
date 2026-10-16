"""Сборка эпизодов и режима сна (ТЗ 27): хранилище `episodes.db`, нарезка, резюме, конвейер сна, планировщик, чтение.

Корень сборки только соединяет готовые части (как `impulse_service`): логика лежит в `domain/episodes.py` и `app/episode_*.py`,
`app/sleep*.py`, `app/diary.py`. Хранилище и поиск у каждого персонажа свои (папка памяти персонажа, ТЗ 05); при переключении
персонажа части создаются заново для его папки, а нарезка нового персонажа начинается с текущего конца журнала (чужую историю
он не нарезает). Модуль памяти объявляет менеджеру фокуса фоновую политику `sleep` (`policies_of` / `status_of` для id `memory`).
Событие `episode.summary` уходит во внимание как событие модуля `memory` и по правилу ТЗ 30 попадает в маршрут think.
"""
import time
from pathlib import Path
from types import SimpleNamespace

import character
import prompts
from adapters.episode_store import FILE, EpisodeStore
from adapters.event_source import JournalSource
from app.diary import DayWriter
from app.episode_cutter import EpisodeCutter
from app.episode_recall import EpisodeRecall
from app.episode_summary import EpisodeSummarizer
from app.sleep import SleepRunner
from app.sleep_scheduler import SleepScheduler
from domain import episodes as dom
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import LmStudioEmbedder

SUMMARY_KIND = "episode.summary"


def summary_event(ep: dict) -> dict:
    """Тело события `episode.summary` (ТЗ 27, ТЗ 30): итог эпизода для размышления. Текст формируют факты эпизода, не реплика."""
    return {"kind": SUMMARY_KIND, "text": f"{ep['title']}. {ep['summary']}"[:400], "urgency_hint": 1, "addressed_to_me": False,
            "memorable": ep["salience"] >= 0.7, "dedupe_key": f"episode:{ep['id']}",
            "data": {"episode": ep["id"], "salience": ep["salience"], "day": ep["day"], "stream": ep["stream"]}}


class EpisodeService:
    def __init__(self, companion, attention, *, settings_source=None, clock=time.time, busy=lambda: False,
                 embedder=None, source=None):
        self.companion, self.attention = companion, attention
        self.settings = settings_source or companion._settings.load
        self.clock = clock
        self.embedder = embedder if embedder is not None else LmStudioEmbedder()
        self.source = source or JournalSource()
        self._parts: dict[str, SimpleNamespace] = {}
        self._dir = ""
        self.scheduler: SleepScheduler | None = None
        self._busy = busy
        self.hooks: list = []                 # сценарии после сна (ТЗ 28: разбор ошибок): async (report)

    # ── части для текущего персонажа ─────────────────────────────────────────

    def parts(self) -> SimpleNamespace:
        folder = Path(self.companion._dir)
        key = str(folder)
        if key != self._dir:
            self._dir = key
            if key not in self._parts:
                self._parts[key] = self._build(folder)
            p = self._parts[key]
            if self.scheduler is not None:
                self.scheduler.interrupt("переключение персонажа")
                self.scheduler.runner, self.scheduler.store = p.runner, p.store
        return self._parts[key]

    @property
    def recall(self) -> EpisodeRecall:
        return self.parts().recall

    @property
    def store(self) -> EpisodeStore:
        return self.parts().store

    def _build(self, folder: Path) -> SimpleNamespace:
        llm = self.companion._llm
        store = EpisodeStore(folder / FILE, self.clock)
        index = HybridIndex(folder / FILE, clock=self.clock)
        search = HybridSearch(index, self.embedder, llm, SearchConfig(recency_weight=0.15, half_life_days=60.0))
        allow = lambda: [character.active().name, *character.active().aliases]            # noqa: E731
        first = character.DEFAULT_ID == character.active().id
        cutter = EpisodeCutter(source=self.source, store=store, settings=self.settings, clock=self.clock,
                               character=lambda: character.active().id,
                               first_upto=None if first else self.source.last_id)
        summarizer = EpisodeSummarizer(llm=llm, prompt=prompts.episode, allow=allow, settings=self.settings)
        days = DayWriter(llm=llm, store=store, search=search, digest_prompt=prompts.digest, diary_prompt=prompts.diary,
                         allow=allow, character=lambda: character.active().id)
        runner = SleepRunner(cutter=cutter, summarizer=summarizer, store=store, search=search,
                             writer=self.companion.memory_writer, days=days, source=self.source, settings=self.settings,
                             clock=self.clock, notify=self._notify, hooks=self.hooks)
        recall = EpisodeRecall(store=store, search=search, source=self.source)
        return SimpleNamespace(store=store, search=search, cutter=cutter, summarizer=summarizer, days=days, runner=runner,
                               recall=recall)

    async def _notify(self, ep: dict) -> None:
        await self.attention.submit(dom.MEMORY_MODULE, summary_event(ep))

    # ── фон ──────────────────────────────────────────────────────────────────

    def attach_scheduler(self) -> SleepScheduler:
        p = self.parts()
        self.scheduler = SleepScheduler(
            p.runner, store=p.store, settings=self.settings, clock=self.clock,
            idle_seconds=lambda: float(self.companion.idle_seconds() or 0.0),
            last_user_ts=lambda: float(getattr(self.companion, "_last_user_ts", 0.0) or 0.0),
            busy=lambda: (self.parts(), self._busy())[1])          # parts(): подхватить смену персонажа
        return self.scheduler

    async def start(self) -> None:
        if self.scheduler is None:
            self.attach_scheduler()
        await self.scheduler.start_loop()

    async def stop(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()

    # ── политика фокуса и дашборд ────────────────────────────────────────────

    def policies_of(self, module_id: str) -> dict | None:
        return SleepScheduler.policies() if module_id == dom.MEMORY_MODULE else None

    def status_of(self, module_id: str) -> dict | None:
        if module_id != dom.MEMORY_MODULE or self.scheduler is None:
            return None
        return self.scheduler.focus_status()

    async def snapshot(self, limit: int = 30) -> dict:
        """Данные вкладки «Память»: статус сна, эпизоды, дневник, сводки."""
        import asyncio
        p = self.parts()
        sched = self.scheduler.status() if self.scheduler is not None else {"state": "off"}
        return {"sleep": sched, "episodes": await p.recall.listing(limit=limit), "diary": await p.recall.diary(limit),
                "digests": await asyncio.to_thread(p.store.digests, limit)}


def build(companion, attention, **kw) -> EpisodeService:
    """Создать сервис и положить его в `companion.episodes` (читают инструменты `recall` и `recall_episode`, адаптер памяти)."""
    service = EpisodeService(companion, attention, **kw)
    service.parts()
    companion.episodes = service
    return service
