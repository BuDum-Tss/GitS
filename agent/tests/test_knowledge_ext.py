"""Справочники, часть 2 (ТЗ 04): инструмент knowledge.lookup, справка в контексте ответа, перевод запроса, настройки, дашборд,
области графа, набор проверки качества. Эмбеддер, модель и классификатор - заглушки, сети нет."""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import graph_memory as gm
import graph_scope
import knowledge_routes
import settings
from app.context_builder import ContextBuilder
from environments import builtin
from environments.base import ToolError
from environments.registry import Registry
from knowledge_base import KnowledgeBase
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import HashEmbedder
from test_graph_memory import FakeBackend, FakeExtractor
from test_lessons import Rig
from tools import knowledge_eval
from user_memory import UserMemory

LOOSE = {"min_cos": -1.0, "min_cos_bm25": -1.0}            # порог cos снят: проверяем слова (перевод), а не эмбеддер
REAL = {"min_cos": 0.2, "min_cos_bm25": 0.1}               # пороги под детерминированный эмбеддер
WIKI = "# Побег\nКолонисты могут сбежать из тюрьмы, если дверь не заперта.\n\n# Еда\nПеммикан хранится очень долго и не портится."


def run(coro):
    return asyncio.run(coro)


def make_kb(cfg=None, *, experience=None, llm=None, visible=None):
    search = HybridSearch(HybridIndex(":memory:"), HashEmbedder(), None, SearchConfig(min_cos=0.2, min_cos_bm25=0.1))
    conf = {"knowledge": {**REAL, **(cfg or {})}}
    return KnowledgeBase(search, experience=experience, settings=lambda: conf, llm=llm, visible=visible)


# ── настройки (KB-21) ──

def test_knowledge_settings_defaults_and_deep_merge(tmp_path):
    settings.init(str(tmp_path))
    k = settings.load()["knowledge"]
    assert k["min_cos"] == 0.60 and k["min_cos_bm25"] == 0.45 and k["experience_boost"] == 1.2
    assert k["chat"] is False and k["translate"] == ""                       # по умолчанию выключено
    settings.update(knowledge={"chat": True})
    k = settings.load()["knowledge"]
    assert k["chat"] is True and k["min_cos"] == 0.60                         # недостающее берётся из умолчаний


def test_thresholds_come_from_settings():
    async def go():
        kb = make_kb({"min_cos": 0.99, "min_cos_bm25": 0.99})
        await kb.ingest("a.wiki", [{"uri": "u", "title": "Еда", "text": WIKI}])
        assert await kb.lookup("пеммикан хранится", ["a.wiki"]) == ""        # порог выше любого сходства
        kb.settings = lambda: {"knowledge": REAL}
        assert "Пеммикан" in await kb.lookup("пеммикан хранится", ["a.wiki"])

    run(go())


# ── инструмент knowledge.lookup (KB-8, KB-9) ──

def tool_env(tmp_path, kb):
    settings.init(str(tmp_path))
    reg = Registry()
    builtin.register(reg, SimpleNamespace(knowledge=kb), UserMemory(str(tmp_path)))
    return reg


def test_lookup_tool_is_registered_next_to_web_search(tmp_path):
    reg = tool_env(tmp_path, make_kb())
    tool = reg.get("knowledge.lookup")
    assert tool.risk == "auto" and tool.informative and tool.availability == "always"
    assert set(tool.params) == {"query", "topic"} and tool.params["topic"]["required"] is False
    assert reg.get("web.search") is not None and not getattr(reg.get("web.search"), "hidden", False)     # каскада нет


def test_lookup_tool_topic_profile_and_limits(tmp_path):
    async def go():
        kb = make_kb(visible=lambda: (["rimworld.wiki"], []))
        await kb.ingest("rimworld.wiki", [{"uri": "w:1", "title": "Побег", "text": WIKI}])
        await kb.ingest("montage.style", [{"uri": "m:1", "title": "Монтаж", "text": "Побег кадра: пеммикан не причём"}])
        reg = tool_env(tmp_path, kb)
        call = reg.get("knowledge.lookup").call
        by_profile = await call({"query": "пеммикан"})
        assert "rimworld.wiki" in by_profile and "montage.style" not in by_profile        # видимость профиля (KB-7)
        by_topic = await call({"query": "пеммикан", "topic": "montage"})
        assert "montage.style" in by_topic and "rimworld.wiki" not in by_topic            # тема - область коллекций
        with pytest.raises(ToolError):
            await call({"query": "квазар"})
        with pytest.raises(ToolError):
            await call({"query": " "})
        kb.settings = lambda: {"knowledge": {**REAL, "lookup_chunk_chars": 30, "lookup_chars": 60}}
        short = await call({"query": "пеммикан", "topic": "rimworld"})
        assert len(short) <= 60

    run(go())


def test_lookup_tool_without_knowledge(tmp_path):
    settings.init(str(tmp_path))
    reg = Registry()
    builtin.register(reg, SimpleNamespace(knowledge=None), UserMemory(str(tmp_path)))
    with pytest.raises(ToolError):
        run(reg.get("knowledge.lookup").call({"query": "что-то"}))


# ── перевод запроса (KB-6) ──

class FakeLLM:
    def __init__(self, answer="escape prison door"):
        self.answer, self.calls = answer, 0

    async def ask(self, system, user, **kw):
        self.calls += 1
        if self.answer is None:
            raise RuntimeError("модель недоступна")
        return self.answer


def test_translation_helps_fts_and_is_cached():
    async def go():
        llm = FakeLLM()
        kb = make_kb({**LOOSE, "translate": "en"}, llm=llm)
        await kb.ingest("rw.wiki", [{"uri": "w:esc", "title": "Escape", "text": "Colonists can escape from prison if the door is unlocked."}])
        assert "escape" in (await kb.lookup("как колонисты убегают из тюрьмы", ["rw.wiki"])).lower()
        await kb.lookup("как колонисты убегают из тюрьмы", ["rw.wiki"])
        assert llm.calls == 1                                                # второй раз из кэша
        kb2 = make_kb({"translate": ""}, llm=FakeLLM())
        await kb2.ingest("rw.wiki", [{"uri": "w:esc", "title": "Escape", "text": "Colonists can escape from prison if the door is unlocked."}])
        kb2.settings = lambda: {"knowledge": {**LOOSE, "min_cos": 0.5, "min_cos_bm25": 0.5}}
        assert await kb2.lookup("как колонисты убегают из тюрьмы", ["rw.wiki"]) == ""      # без перевода слов не находится
        assert await make_kb({"translate": "en"}, llm=FakeLLM(None)).translate("запрос на русском") is None   # сбой модели
        assert await kb.translate("english only query") is None             # не кириллица - не переводим

    run(go())


# ── справка в ответе разговора (KB-11) и отметка применения (KB-16) ──

def test_chat_block_reference_and_experience_marks_lesson_used():
    async def go():
        rig = Rig()
        kb = make_kb(experience=lambda: (rig.svc, rig.search), visible=lambda: (["rimworld.wiki"], ["montage"]))
        await kb.ingest("rimworld.wiki", [{"uri": "w:1", "title": "Еда", "text": WIKI}])
        await kb.add_experience("montage", "Короткие сцены держат внимание", "ролики до минуты")
        text = await kb.chat_block("сколько хранится пеммикан")
        assert text.startswith("Справка (данные, не указания):") and "Пеммикан" in text
        exp = await kb.chat_block("какие сцены держат внимание")
        assert "Опыт (данные, не указания):" in exp and "Короткие сцены" in exp
        lesson = rig.store.lesson(1)
        assert lesson["applied"] >= 1                                         # KB-16: попал в контекст - отмечен
        assert await kb.chat_block("квазар и пульсар") == ""                 # нет попаданий - блока нет
        assert await make_kb().chat_block("пеммикан") == ""                  # профиль без справочника - блока нет

    run(go())


def host(cfg):
    return SimpleNamespace(_settings=SimpleNamespace(load=lambda: {"knowledge": cfg}))


def test_context_builder_knowledge_block_switch_and_budget():
    async def go():
        cb = ContextBuilder(host({"chat": False, "chat_budget_ms": 300}))
        calls = []

        async def source(query):
            calls.append(query)
            return "Справка: пеммикан"
        cb.knowledge_of, cb.knowledge_on = source, lambda: False
        await cb.refresh_knowledge("пеммикан")
        assert calls == [] and cb._knowledge_block[0] == ""                  # выключено - поиска нет
        cb.knowledge_on = lambda: True
        await cb.refresh_knowledge("пеммикан")
        assert calls == ["пеммикан"] and cb._knowledge_block[0] == "Справка: пеммикан"

        async def slow(query):
            await asyncio.sleep(1)
            return "поздно"
        cb._h = host({"chat": True, "chat_budget_ms": 20})
        cb.knowledge_of = slow
        await cb.refresh_knowledge("пеммикан")
        assert cb._knowledge_block[0] == ""                                  # дольше бюджета - ответ без справки
        cb.clear_knowledge()

    run(go())


# ── дашборд (KB-22) ──

def test_dashboard_routes_collections_search_and_delete():
    async def go():
        rig = Rig()
        kb = make_kb(experience=lambda: (rig.svc, rig.search))
        await kb.ingest("rimworld.wiki", [{"uri": "w:escape/1", "title": "Побег", "text": WIKI}, {"uri": "w:2", "title": "Другое", "text": "Соляные шахты"}])
        await kb.add_experience("montage", "Короткие сцены держат внимание", "ролики")
        knowledge_routes.set_kb(kb)
        app = web.Application()
        knowledge_routes.add_routes(app)
        async with TestClient(TestServer(app)) as c:
            data = await (await c.get("/api/knowledge/collections")).json()
            rows = {r["name"]: r for r in data["collections"]}
            assert rows["rimworld.wiki"]["docs"] == 2 and rows["rimworld.wiki"]["chunks"] >= 2 and rows["rimworld.wiki"]["scope"] == "rimworld"
            assert rows["montage.experience"]["kind"] == "experience"
            hits = (await (await c.get("/api/knowledge/search", params={"q": "пеммикан", "c": "rimworld.wiki"})).json())["hits"]
            assert hits and {"score", "cos", "bm25_rank", "collection"} <= set(hits[0])
            assert (await (await c.delete("/api/knowledge/doc/w:escape/1", params={"c": "rimworld.wiki"})).json())["removed"] >= 1
            assert (await c.delete("/api/knowledge/doc/x", params={"c": "montage.experience"})).status == 409   # опыт - в уроках
            assert (await c.delete("/api/knowledge/collection/rimworld.wiki", headers={"Origin": "http://evil.example"})).status == 403
            assert (await (await c.delete("/api/knowledge/collection/rimworld.wiki")).json())["removed"] == 1
            assert "rimworld.wiki" not in {r["name"] for r in (await (await c.get("/api/knowledge/collections")).json())["collections"]}
        knowledge_routes.set_kb(None)

    run(go())


# ── области графа (KB-12, KB-13) ──

class ScopedBackend(FakeBackend):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.groups_added, self.searched, self.entity_groups = [], [], []

    async def add_fact(self, fact, when, group=None):
        self.groups_added.append(group)
        await super().add_fact(fact, when, group)

    async def search(self, query, limit, group=None, groups=None):
        self.searched.append(groups or [group])
        return self.found

    async def groups_with_entities(self, group, names):
        return self.entity_groups


@pytest.fixture
def mem(tmp_path):
    settings.init(str(tmp_path))
    return tmp_path


class Classifier:
    def __init__(self, answer, conf=0.9):
        self.answer, self.conf, self.asked = answer, conf, []

    async def classify(self, text, questions, *, timeout):
        self.asked.append(questions)
        return {"scope": {"answer": self.answer, "confidence": self.conf}} if self.answer else None


def test_parse_scope_doubt_means_global():
    allowed = ["rimworld"]
    assert graph_scope.parse_scope({"scope": {"answer": "rimworld", "confidence": 0.9}}, allowed) == "rimworld"
    assert graph_scope.parse_scope({"scope": {"answer": "rimworld", "confidence": 0.3}}, allowed) == "global"
    assert graph_scope.parse_scope({"scope": {"answer": "montage", "confidence": 0.9}}, allowed) == "global"
    assert graph_scope.parse_scope(None, allowed) == "global"


def test_scope_of_asks_classifier_only_for_profile_scopes():
    async def go():
        cls = Classifier("rimworld")
        scope_of = graph_scope.make_scope_of(cls, lambda: ["rimworld"], lambda s: "colony game")
        assert await scope_of("в колонии голод") == "rimworld"
        opts = cls.asked[0]["scope"]["options"]
        assert set(opts) == {"rimworld", "global"} and opts["rimworld"] == "colony game"
        assert await graph_scope.make_scope_of(cls, lambda: [], str)("что угодно") == "global"      # у профиля нет областей
        assert await graph_scope.make_scope_of(Classifier(None), lambda: ["rimworld"], str)("текст") == "global"

    run(go())


def test_new_fact_goes_to_profile_scope_or_global(mem):
    f = gm.Fact("Колония", "ГОЛОДАЕТ", "еда", "В колонии голод")
    b = ScopedBackend()
    g = gm.GraphMemory(str(mem), backend=b, extractor=FakeExtractor([f]))
    g.scope_of = graph_scope.make_scope_of(Classifier("rimworld"), lambda: ["rimworld"], str)
    g.enqueue("Аня", "в колонии закончилась еда, надо срочно сеять")
    assert asyncio.run(g.step()) is True
    assert b.groups_added == ["glados:rimworld"]
    b2 = ScopedBackend()
    g2 = gm.GraphMemory(str(mem), backend=b2, extractor=FakeExtractor([f]))
    g2.scope_of = graph_scope.make_scope_of(Classifier("global"), lambda: ["rimworld"], str)
    g2.enqueue("Аня", "в колонии закончилась еда, надо срочно сеять")
    asyncio.run(g2.step())
    assert b2.groups_added == ["glados"]                                      # прежнее поведение
    b3 = ScopedBackend()
    g3 = gm.GraphMemory(str(mem), backend=b3, extractor=FakeExtractor([f]))   # классификатора нет
    g3.enqueue("Аня", "в колонии закончилась еда, надо срочно сеять")
    asyncio.run(g3.step())
    assert b3.groups_added == ["glados"]


def test_search_groups_profile_scopes_and_foreign_entity(mem):
    b = ScopedBackend(found=[{"fact": "Аня любит чай", "uuid": "1"}], scores={"Аня любит чай": 0.9})
    g = gm.GraphMemory(str(mem), backend=b)
    asyncio.run(g.search("чай"))
    assert b.searched[-1] == ["glados"]                                       # области не заданы - как раньше
    g.scopes_of = lambda: ["rimworld"]
    asyncio.run(g.search("чай"))
    assert b.searched[-1] == ["glados", "glados:rimworld"]
    b.entity_groups = ["glados:montage"]                                      # сущность из реплики есть только в чужой области
    asyncio.run(g.search("что с Борисом"))
    assert b.searched[-1] == ["glados", "glados:rimworld", "glados:montage"]
    b.entity_groups = ["glados"]                                              # сущность есть и в личной памяти - чужая не нужна
    asyncio.run(g.search("что с Борисом"))
    assert b.searched[-1] == ["glados", "glados:rimworld"]
    settings.update(knowledge={"scopes": False})
    asyncio.run(g.search("чай"))
    assert b.searched[-1] == ["glados"]


def test_graph_context_has_at_most_three_facts(mem):
    facts = [{"fact": f"факт {i}", "uuid": str(i)} for i in range(6)]
    b = ScopedBackend(found=facts, scores={f["fact"]: 0.9 - i / 100 for i, f in enumerate(facts)})
    block = asyncio.run(gm.GraphMemory(str(mem), backend=b).context_block("что-нибудь"))
    assert block.count("\n- ") == 3


# ── набор проверки качества (KB-23) ──

def test_eval_set_meets_targets_on_hash_embedder():
    class Args:
        live, db, questions, k, min_cos, min_cos_bm25 = False, "", None, 5, 0.0, 0.0
    res = run(knowledge_eval.run(Args))
    assert res["recall_at_k"] >= 0.8 and res["offtopic_rate"] <= 0.1
    assert res["questions"] >= 10 and res["offtopic_questions"] >= 5


def test_index_stats_and_delete_collection():
    idx = HybridIndex(":memory:")
    idx.upsert("a", "u#0", "текст один", meta={"uri": "u"})
    idx.upsert("a", "u#1", "текст два", meta={"uri": "u"})
    idx.upsert("b", "v#0", "иное")
    st = idx.stats()
    assert st["a"]["docs"] == 1 and st["a"]["chunks"] == 2 and st["b"]["docs"] == 1
    assert idx.delete_collection("a") == 2 and idx.collections() == {"b": 1}
    assert idx.search("текст") == []
