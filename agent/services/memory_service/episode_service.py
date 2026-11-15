"""Сборка эпизодов и режима сна (ТЗ 27): хранилище `episodes.db`, выбор моментов, рассказ, конвейер сна, планировщик, чтение.

Корень сборки только соединяет готовые части (как `impulse_service`): логика лежит в `services/memory_service/episodes.py` и `app/episode_*.py`,
`app/sleep*.py`, `services/memory_service/diary.py`. Хранилище и поиск у каждого персонажа свои (папка памяти персонажа, ТЗ 05); при переключении
персонажа части создаются заново для его папки, а выбор моментов нового персонажа начинается с текущего конца журнала (чужая история
сама не обрабатывается; для неё есть `selector.backfill`).
Событие `episode.summary` уходит во внимание как событие модуля `memory` и по правилу ТЗ 30 попадает в маршрут think.
"""
import time
from pathlib import Path
from types import SimpleNamespace

from services.character import character
from services.character import prompts
from services.runtime import pi_llm
from services.memory_service.episode_store import FILE, EpisodeStore
from services.tracing import events_log
from services.memory_service.diary import DayWriter
from services.memory_service.moment_selector import MomentSelector
from services.memory_service.episode_recall import EpisodeRecall
from services.memory_service.episode_fork import ForkedEpisodeWriter
from services.memory_service.episode_summary import EpisodeSummarizer
from services.memory_service.live_episodes import LiveEpisodes
from services.memory_service.sleep import SleepRunner
from services.memory_service.sleep_scheduler import SleepScheduler
import services.memory_service.episodes as dom
from services.memory_service.search import HybridIndex, HybridSearch, SearchConfig
from services.memory_service.search.embedder import LmStudioEmbedder

SUMMARY_KIND = "episode.summary"
SESSION_FILE = "resident_pi_session.json"     # сессия её агента pi в папке памяти персонажа (форк копирует её)


def summary_event(ep: dict) -> dict:
    """Тело события `episode.summary` (ТЗ 27, ТЗ 30): итог эпизода для размышления. Текст формируют факты эпизода, не реплика."""
    return {"kind": SUMMARY_KIND, "text": f"{ep['title']}. {ep['summary']} {ep.get('attitude') or ''}".strip()[:400], "urgency_hint": 1, "addressed_to_me": False,
            "memorable": ep["salience"] >= 0.7, "dedupe_key": f"episode:{ep['id']}",
            "data": {"episode": ep["id"], "salience": ep["salience"], "day": ep["day"], "stream": ep["stream"]}}


class EpisodeService:
    def __init__(self, companion, attention, *, settings_source=None, clock=time.time, busy=lambda: False,
                 embedder=None, source=None):
        self.companion, self.attention = companion, attention
        self.settings = settings_source or companion._settings.load
        self.clock = clock
        self.embedder = embedder if embedder is not None else LmStudioEmbedder()
        self.source = source or events_log
        self._parts: dict[str, SimpleNamespace] = {}
        self._dir = ""
        self.scheduler: SleepScheduler | None = None
        self.live: LiveEpisodes | None = None
        self._busy = busy
        self.hooks: list = []                 # сценарии после сна (ТЗ 28: разбор ошибок): async (report)

    # ── части для текущего персонажа ─────────────────────────────────────────

    def _thought_notes(self, day: str) -> list[str]:
        """Мысли дня (ГГГГ-ММ-ДД), перенесённые в долговременную память: дополнительный материал дневника."""
        import time as _t
        impulses = getattr(self.companion, "impulses", None)
        if impulses is None:
            return []
        out = []
        for t in reversed(impulses.journal.recent(200)):
            if t.get("stage") == "long" and _t.strftime("%Y-%m-%d", _t.localtime(float(t.get("ts") or 0))) == day and t.get("text"):
                out.append(str(t["text"])[:200])
        return out

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
        selector = MomentSelector(source=self.source, store=store, settings=self.settings, clock=self.clock,
                                  character=lambda: character.active().id)        # без отметки начинает с конца журнала
        base = EpisodeSummarizer(llm=llm, prompt=prompts.episode, allow=allow, settings=self.settings, heavy=self._heavy)
        summarizer = ForkedEpisodeWriter(                         # пишет форк её сессии; запасной путь: heavy или fast, строгая проверка
            fallback=base, session_path=lambda: Path(self.companion._dir) / SESSION_FILE, system=self._system_prompt,
            busy=self._live_busy, settings=self.settings, allow=allow)
        days = DayWriter(llm=llm, store=store, search=search, digest_prompt=prompts.digest, diary_prompt=prompts.diary,
                         allow=allow, character=lambda: character.active().id, notes=self._thought_notes)
        runner = SleepRunner(selector=selector, summarizer=summarizer, store=store, search=search,
                             writer=self.companion.memory_writer, days=days, source=self.source, settings=self.settings,
                             clock=self.clock, notify=self._notify, hooks=self.hooks)
        recall = EpisodeRecall(store=store, search=search, source=self.source)
        return SimpleNamespace(store=store, search=search, selector=selector, summarizer=summarizer, days=days, runner=runner,
                               recall=recall)

    def _system_prompt(self) -> str:
        """Системный промпт её агента (тот же, что в её ходах): форк пишет её голосом."""
        resident = getattr(self.companion, "resident", None)
        return resident._system() if resident is not None else prompts.companion_persona()

    def _live_busy(self) -> bool:
        """Занята: идёт разговор, её ход или речь (тогда эпизод не пишется)."""
        c = self.companion
        resident = getattr(c, "resident", None)
        speaking = getattr(c, "is_speaking", None)
        return bool(self._busy() or (resident is not None and resident.busy) or (callable(speaking) and speaking()))

    @staticmethod
    async def _heavy(system: str, user: str) -> str:
        """Сильная модель pi (`pi_model_heavy`) для записи эпизодов; сбой уходит в резюме на быстрой модели."""
        return await pi_llm.ask_heavy(system, user, 120.0)

    async def _notify(self, ep: dict) -> None:
        await self.attention.submit(dom.MEMORY_MODULE, summary_event(ep))

    # ── фон ──────────────────────────────────────────────────────────────────

    def attach_scheduler(self) -> SleepScheduler:
        p = self.parts()
        self.scheduler: SleepScheduler = SleepScheduler(
            p.runner, store=p.store, settings=self.settings, clock=self.clock,
            idle_seconds=lambda: float(self.companion.idle_seconds() or 0.0),
            last_user_ts=lambda: float(getattr(self.companion, "_last_user_ts", 0.0) or 0.0),
            busy=lambda: (self.parts(), self._busy())[1])          # parts(): подхватить смену персонажа
        return self.scheduler

    async def start(self) -> None:
        if self.scheduler is None:
            self.attach_scheduler()
        await self.scheduler.start_loop()
        if self.live is None:
            self.live = LiveEpisodes(self.parts, settings=self.settings, clock=self.clock, source=self.source, busy=self._live_busy,
                                     asleep=lambda: self.scheduler is not None and self.scheduler.state == "sleeping")
        await self.live.start()                           # эпизоды пишутся и пока она бодрствует (сон: догон, дневник, сводки)

    async def stop(self) -> None:
        if self.live is not None:
            await self.live.stop()
        if self.scheduler is not None:
            await self.scheduler.stop()

    # ── дашборд ──────────────────────────────────────────────────────────────

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
