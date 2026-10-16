"""Сборка учёбы на ошибках (ТЗ 28): уроки, убеждения, отмена, подбор для контекста.

Корень сборки только соединяет готовые части (как `episode_service`): логика в `domain/lessons.py` и `app/lessons.py`, хранилище в
`adapters/lesson_store.py`. У каждого персонажа свои уроки и убеждения (папка памяти персонажа, ТЗ 05; файл `lessons.db`). Общий
поиск и номера эпизодов берутся у `EpisodeService`: шаг разбора ошибок встаёт в его `hooks` (после сна, ТЗ 27, EPI-5)."""
import time
from pathlib import Path

import events_log
import prompts
from adapters.lesson_store import FILE, LessonStore
from app.lessons import LessonService


def _journal(kind: str, text: str = "", **data) -> None:
    events_log.log_nowait(kind, text, **data)


class LessonHub:
    def __init__(self, companion, episodes, *, settings_source=None, clock=time.time, log=_journal):
        self.companion, self.episodes, self.clock, self._log = companion, episodes, clock, log
        self.settings = settings_source or companion._settings.load
        self._parts: dict[str, LessonService] = {}

    def service(self) -> LessonService:
        """Сервис текущего персонажа (создаётся при первом обращении; поиск и эпизоды берутся из его частей эпизодов)."""
        p = self.episodes.parts()                     # подхватывает смену персонажа
        key = str(Path(self.companion._dir))
        if key not in self._parts:
            store = LessonStore(Path(key) / FILE, self.clock)
            self._parts[key] = LessonService(
                store=store, llm=self.companion._llm, search=p.search, source=self.episodes.source, episodes_of=p.store.episodes_of_event,
                settings=self.settings, prompt_lesson=prompts.lesson, prompt_check=prompts.belief_check,
                constitution=prompts.constitution_text, clock=self.clock, log=self._log)
        return self._parts[key]

    # ── точки входа ядра ─────────────────────────────────────────────────────

    async def review(self, report: dict) -> None:
        """Шаг после сна (`EpisodeService.hooks`)."""
        await self.service().review(report)

    async def select(self, query: str) -> str:
        """Блок уроков по ситуации для ContextBuilder (пусто, пока настройка `lessons.context` выключена)."""
        return await self.service().select(query)

    def beliefs_block(self) -> str:
        return self.service().beliefs_block()

    def attach(self, context_builder) -> None:
        """Встроить в сон и в контекст ответа."""
        self.episodes.hooks.append(self.review)
        context_builder.lessons_of = self.select
        context_builder.beliefs_of = self.beliefs_block


def build(companion, episodes, **kw) -> LessonHub:
    hub = LessonHub(companion, episodes, **kw)
    hub.attach(companion.context_builder)
    companion.lessons = hub
    return hub

