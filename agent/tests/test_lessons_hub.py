"""Сборка учёбы на ошибках (ТЗ 28): хаб встаёт в сон и в контекст, у персонажей свои уроки."""
import asyncio
from types import SimpleNamespace

import lesson_service
from app.context_builder import ContextBuilder
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import HashEmbedder
from test_lessons import LLM, Clock, Host, Journal


def test_hub_wires_sleep_hook_context_and_per_character_stores(tmp_path):
    logs = []
    clock = Clock()
    llm = LLM()
    search = HybridSearch(HybridIndex(":memory:", clock=clock), HashEmbedder(), llm, SearchConfig(min_cos=0.2, min_cos_bm25=0.1))
    journal = Journal()
    eps = SimpleNamespace(hooks=[], source=journal, parts=lambda: SimpleNamespace(
        search=search, store=SimpleNamespace(episodes_of_event=lambda eid: ["epX"])))
    companion = SimpleNamespace(_dir=tmp_path / "a", _llm=llm, context_builder=ContextBuilder(Host()),
                                _settings=SimpleNamespace(load=lambda: {"lessons": {"context": True}}))
    hub = lesson_service.build(companion, eps, clock=clock, log=lambda kind, text="", **d: logs.append(kind))
    assert eps.hooks == [hub.review] and companion.lessons is hub
    assert companion.context_builder.lessons_of == hub.select and companion.context_builder.beliefs_of == hub.beliefs_block
    journal.mistake(clock.t, "не удалось запустить блокнот: приложение не найдено")
    rep = {}

    async def go():
        for hook in eps.hooks:
            await hook(rep)
        return await hub.select("запусти блокнот")

    assert "блокнот" in asyncio.run(go()) and rep["lessons"]["lessons_new"] == 1 and "lesson" in logs
    first = hub.service()
    assert (tmp_path / "a" / "lessons.db").exists()
    companion._dir = tmp_path / "b"                                  # другой персонаж: свои уроки
    assert hub.service() is not first and hub.service().store.lessons() == []
