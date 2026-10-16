"""Справочники и опыт (ТЗ 04, KB-1..KB-16, KB-24): разбиение, загрузка, поиск, права, опыт. Эмбеддер - заглушка, сети нет."""
import asyncio
from types import SimpleNamespace

import pytest

from core_requests import CoreRequests
from knowledge_base import KnowledgeBase, chunk
from module_hub import ModuleError
from search import HybridIndex, HybridSearch, SearchConfig
from search.embedder import HashEmbedder
from test_lessons import Rig


def run(coro):
    return asyncio.run(coro)


def make_kb(experience=None):
    search = HybridSearch(HybridIndex(":memory:"), HashEmbedder(), None, SearchConfig(min_cos=0.2, min_cos_bm25=0.1))
    return KnowledgeBase(search, experience=experience)


WIKI = "# Побег\nКолонисты могут сбежать из тюрьмы, если дверь не заперта.\n\n# Еда\nПеммикан хранится очень долго и не портится."


def test_chunk_splits_by_headings_and_long_sections():
    assert chunk("") == []
    short = chunk(WIKI, "Вики")
    assert len(short) == 1 and short[0].startswith("Вики\n")                      # короткие разделы склеиваются
    big = "# А\n" + " ".join(f"слово{i}" for i in range(1000))
    pieces = chunk(big)
    assert len(pieces) >= 3 and all(len(p.split()) <= 450 for p in pieces)


def test_ingest_lookup_and_replace():
    async def go():
        kb = make_kb()
        res = await kb.ingest("rimworld.wiki", [{"uri": "w:escape", "title": "Побег", "text": WIKI}])
        assert res["added"] == 1 and res["skipped"] == 0
        assert (await kb.ingest("rimworld.wiki", [{"uri": "w:escape", "title": "Побег", "text": WIKI}]))["updated"] == 1
        text = await kb.lookup("сколько хранится пеммикан", ["rimworld.wiki"])
        assert "Пеммикан" in text and "Побег" in text and "rimworld.wiki" in text         # источник указан
        assert await kb.lookup("пеммикан", ["montage.style"]) == ""                      # чужая коллекция не видна
        await kb.ingest("rimworld.wiki", [{"uri": "w:new", "title": "Новое", "text": "Совсем другое про соляные шахты"}],
                        replace=True, final=True)
        assert await kb.collections() == {"rimworld.wiki": 1}                             # старый документ заменён
        res = await kb.ingest("rimworld.wiki", [{"uri": "", "text": "x"}, {"uri": "u", "text": " "}])
        assert res["skipped"] == 2

    run(go())


def test_collection_rights():
    manifest = {"knowledge": [{"collection": "rimworld.wiki", "title": "t", "scope": "rimworld", "kind": "reference"}]}
    assert KnowledgeBase.allowed("rimworld", manifest, "rimworld.wiki")
    assert KnowledgeBase.allowed("rimworld", {}, "rimworld.defs")                         # свой префикс
    assert not KnowledgeBase.allowed("rimworld", manifest, "montage.style")


def test_experience_goes_to_lessons_and_is_found_by_lookup():
    async def go():
        rig = Rig()
        kb = make_kb(experience=lambda: (rig.svc, rig.search))
        r = await kb.add_experience("montage", "Короткие сцены держат внимание", "ролики до минуты")
        assert r["stored"] is True
        again = await kb.add_experience("montage", "Короткие сцены держат внимание", "ролики до минуты")
        assert again["stored"] is False and again["merged"] is True                       # слияние, а не дубль
        text = await kb.lookup("какие сцены держат внимание", ["montage.experience"])
        assert "Короткие сцены" in text
        return kb

    run(go())


def test_core_requests_lookup_ingest_experience_and_denied():
    async def go():
        rig = Rig()
        kb = make_kb(experience=lambda: (rig.svc, rig.search))
        req = CoreRequests()
        conn = SimpleNamespace(module_id="rimworld", manifest={"knowledge": [{"collection": "rimworld.wiki"}]})
        with pytest.raises(ModuleError) as e:
            await req.handle(conn, "core.lookup", {"query": "x"})
        assert e.value.code == "unavailable"                                              # справочник не подключён
        req.knowledge = kb
        out = await req.handle(conn, "core.kb.ingest", {"collection": "rimworld.wiki", "final": True, "replace": True,
                                                         "documents": [{"uri": "a", "title": "Побег", "text": WIKI}]})
        assert out["stored"] == 1
        with pytest.raises(ModuleError) as e:
            await req.handle(conn, "core.kb.ingest", {"collection": "montage.style", "documents": [{"uri": "a", "text": "t"}]})
        assert e.value.code == "denied"
        assert "Пеммикан" in (await req.handle(conn, "core.lookup", {"query": "пеммикан"}))["text"]
        res = await req.handle(conn, "core.experience.add", {"scope": "rimworld", "statement": "Рейд приходит после 200 очков угрозы"})
        assert res["stored"] is True
        with pytest.raises(ModuleError) as e:
            await req.handle(conn, "core.lookup", {})
        assert e.value.code == "bad_request"

    run(go())


def test_ingest_script_parses_wiki_defs_and_loads_folder(tmp_path):
    from tools import knowledge_ingest as ki
    assert ki.wiki_to_text("== Побег ==\n'''Колонист''' бежит [[Тюрьма|из тюрьмы]]{{шаблон}}") == "## Побег\nКолонист бежит из тюрьмы"
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "Побег.wiki").write_text("== Побег ==\nКолонисты могут сбежать из тюрьмы.", encoding="utf-8")
    docs = ki.dir_documents(tmp_path / "wiki")
    assert docs[0]["uri"] == "Побег.wiki" and "сбежать" in docs[0]["text"]
    (tmp_path / "defs").mkdir()
    (tmp_path / "defs" / "a.xml").write_text(
        "<Defs><ThingDef ParentName='Base'><defName>Pemmican</defName><label>пеммикан</label>"
        "<description>Хранится долго.</description></ThingDef><ThingDef><defName>NoText</defName></ThingDef></Defs>", encoding="utf-8")
    defs = ki.defs_documents(tmp_path / "defs")
    assert len(defs) == 1 and "Родитель: Base" in defs[0]["text"] and defs[0]["uri"] == "def:ThingDef:Pemmican"
    assert any(d["uri"] == "montage:craft" for d in ki.montage_documents())

    async def go():
        kb = make_kb()
        res = await ki.ingest(kb, "rimworld.defs", defs, keep=False)
        assert res["added"] == 1
        assert "Хранится" in await kb.lookup("пеммикан хранится", ["rimworld.defs"])
        res = await ki.ingest(kb, "rimworld.defs", [], keep=False)                       # пустая загрузка не падает
        assert res["added"] == 0

    run(go())
