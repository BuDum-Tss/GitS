"""Гибридный поиск (ТЗ 04): FTS5 + векторы + фильтры + свежесть, нормализация, переписывание запроса, бюджет контекста,
стенд memory_bakeoff. Эмбеддер и модель: детерминированные заглушки, сеть не нужна."""
from app.context_builder import ContextBuilder
from contracts import Embedder
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import HashEmbedder
from search.normalize import fts_query, stem, tokens
from search.rewrite import rewrite_query
from tools.memory_bakeoff import run_all

DAY = 86400.0
CFG = SearchConfig(min_cos=0.3, min_cos_bm25=0.1)


def test_russian_forms_share_a_stem():
    assert stem("отпуске") == stem("отпуска") == stem("отпуском")
    assert "что" not in tokens("что я говорил")
    assert fts_query('он сказал "привет"').count("OR") >= 1 and '""' not in fts_query("слово")


async def make(clock=lambda: 1000 * DAY, **cfg):
    s = HybridSearch(HybridIndex(":memory:", clock=clock), HashEmbedder(), cfg=SearchConfig(min_cos=0.3, min_cos_bm25=0.1, **cfg))
    return s


async def test_hash_embedder_satisfies_contract():
    assert isinstance(HashEmbedder(), Embedder)


async def test_collections_filter_and_threshold():
    s = await make()
    await s.add("facts", "a", "Маша моя сестра и живёт в Казани")
    await s.add("reference", "b", "Размер спальни влияет на настроение колонистов")
    hits = await s.search("Кто такая Маша?", collections=["facts"])
    assert [h.key for h in hits] == ["a"]
    assert await s.search("Маша", collections=["reference"]) == []
    assert await s.search("квантовая хромодинамика") == []


async def test_superseded_fact_is_hidden_unless_stale_requested():
    s = await make()
    await s.add("facts", "old", "Мы живём в Самаре", ts=1 * DAY)
    await s.add("facts", "new", "Мы переехали в Казань, раньше жили в Самаре", ts=900 * DAY, supersedes=["old"])
    assert [h.key for h in await s.search("где мы живём в Самаре")] == ["new"]
    assert {h.key for h in await s.search("где мы живём в Самаре", include_stale=True)} == {"old", "new"}


async def test_time_and_meta_filters():
    s = await make()
    await s.add("episodes", "e1", "поход на озеро с Димой", ts=10 * DAY, meta={"who": "Дима"})
    await s.add("episodes", "e2", "поход на озеро с Леной", ts=500 * DAY, meta={"who": "Лена"})
    assert {h.key for h in await s.search("поход на озеро")} == {"e1", "e2"}
    assert [h.key for h in await s.search("поход на озеро", since=100 * DAY)] == ["e2"]
    assert [h.key for h in await s.search("поход на озеро", meta={"who": "Дима"})] == ["e1"]


async def test_recency_weight_prefers_fresh_and_boost_collection():
    s = await make(recency_weight=0.5, half_life_days=30.0)
    await s.add("facts", "old", "встреча команды проходит по понедельникам", ts=100 * DAY)
    await s.add("facts", "fresh", "встреча команды проходит по четвергам", ts=995 * DAY)
    assert (await s.search("встреча команды"))[0].key == "fresh"
    s2 = await make(boosts={"experience": 5.0})
    await s2.add("facts", "f", "кухню строить из камня")
    await s2.add("experience", "x", "кухню строить из камня")
    assert (await s2.search("кухню строить из камня"))[0].key == "x"


async def test_embedder_failure_falls_back_to_fts():
    class Broken:
        async def embed(self, texts, query=False):
            raise RuntimeError("нет сети")

    s = HybridSearch(HybridIndex(":memory:"), Broken())
    await s.add("facts", "a", "Кот по имени Бублик рыжий")
    assert [h.key for h in await s.search("как зовут кота Бублик")] == ["a"]


async def test_rewrite_uses_tail_and_survives_failures():
    class LLM:
        def __init__(self, answer):
            self.answer, self.calls = answer, 0

        async def ask(self, system, user, **kw):
            self.calls += 1
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    llm = LLM("«Когда был поход на озеро?»\nлишнее")
    assert await rewrite_query(llm, "а когда это было?", ["мы ходили в поход на озеро"]) == "Когда был поход на озеро?"
    assert await rewrite_query(llm, "а когда это было?", []) == "а когда это было?" and llm.calls == 1
    assert await rewrite_query(LLM(RuntimeError()), "что там", ["x"]) == "что там"
    assert await rewrite_query(LLM("слово " * 40), "что там", ["x"]) == "что там"
    assert await rewrite_query(None, "что там", ["x"]) == "что там"


class _Mem:
    def __init__(self):
        self.queries = []

    def context_block(self, dialogue=6):
        return "ПАМЯТЬ " + "я" * 600

    def profile_block(self):
        return "ПРОФИЛЬ"

    def graph_status(self):
        return None


class _Host:
    _graph_block = "ГРАФ"
    _self_block = "СЕБЯ"

    class _settings:
        cfg = {"hearing": False}

        @classmethod
        def load(cls):
            return cls.cfg

    class _people:
        @staticmethod
        def describe(_):
            return ""

    def __init__(self):
        self._memory_reader = _Mem()

    def _capabilities(self):
        return "СПРАВКА"

    def recent_heard(self):
        return ""


def test_context_builder_default_is_unchanged_and_modes_budget_work():
    h = _Host()
    b = ContextBuilder(h)
    default = b.build(query="x")
    assert "ГРАФ" in default and "СЕБЯ" in default and "ПРОФИЛЬ" in default
    skipped = b.build(query="x", needs_memory="no")
    assert "ГРАФ" not in skipped and "ПРОФИЛЬ" in skipped            # профиль владельца остаётся и без темы
    pulled = b.build(query="x", mode="pull")
    assert "ГРАФ" not in pulled and "СЕБЯ" not in pulled
    h._settings.cfg = {"hearing": False, "context": {"mode": "push", "budget_tokens": {"reply": {"dialogue": 50}}}}
    try:
        trimmed = b.build(query="x")
        assert len(trimmed) < len(default) and "…" in trimmed and "ГРАФ" in trimmed
        assert len(b.build(query="x", role="other")) == len(default)         # роль без бюджета: как раньше
    finally:
        h._settings.cfg = {"hearing": False}


async def test_bakeoff_stand_runs_offline():
    rep = await run_all("hash")
    rows = {r["variant"]: r for r in rep["rows"]}
    assert rep["questions"] >= 40 and rep["no_answer_questions"] >= 5
    assert rows["hybrid"]["recall_at_5"] >= 0.8
    assert rows["hybrid"]["empty_when_no_answer"] >= 0.9
    assert rows["hybrid"]["stale_correct"] > rows["hybrid_no_supersede"]["stale_correct"]
