"""Хранилище фактов на SQLite: устаревание, повторы, канон, области, воспоминания персонажа и поиск (без LM Studio)."""
from datetime import datetime, timedelta, timezone

import pytest

import graph_memory as gm
from fact_store import FactStore
from graph_memory import COLONIST_RELATIONS, Fact, SqliteBackend
from search.embedder import HashEmbedder

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def backend(tmp_path):
    return SqliteBackend(tmp_path / "graph.db", embedder=HashEmbedder())


async def test_single_valued_relation_closes_the_old_fact(backend):
    await backend.add_fact(Fact("Аня", "ЖИВЁТ_В", "Москва", "Аня живёт в Москве"), T0, "glados")
    await backend.add_fact(Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня переехала в Казань и живёт в Казани"), T0 + timedelta(days=5), "glados")
    edges = {e["to"]: e for e in (await backend.snapshot(group="glados"))["edges"]}
    assert edges["Москва"]["invalid_at"] == "2026-10-06" and not edges["Казань"]["invalid_at"]
    found = {f["fact"]: f for f in await backend.search("Аня живёт в Казани", 5, "glados")}
    assert not found["Аня переехала в Казань и живёт в Казани"]["invalid_at"]


async def test_multi_valued_relation_accumulates_and_exact_repeat_is_not_duplicated(backend):
    u1 = await backend.add_fact(Fact("Аня", "ДРУГ", "Вася", "Вася друг Ани"), T0, "glados")
    await backend.add_fact(Fact("Аня", "ДРУГ", "Катя", "Катя друг Ани"), T0, "glados")
    again = await backend.add_fact(Fact("Аня", "ДРУГ", "Вася", "Вася друг Ани"), T0, "glados")
    assert again == u1 and backend.store.count("glados") == 2
    assert all(not f["invalid_at"] for f in await backend.search("друг Ани", 5, "glados"))


async def test_search_finds_by_meaning_words_and_stays_in_the_group(backend):
    await backend.add_fact(Fact("Аня", "ВЛАДЕЕТ", "кот", "У Ани есть рыжий кот Барсик"), T0, "glados")
    await backend.add_fact(Fact("Аня", "ВЛАДЕЕТ", "кот", "У Ани в чужой области есть кот"), T0, "other")
    facts = [f["fact"] for f in await backend.search("рыжий кот Барсик", 5, "glados")]
    assert facts == ["У Ани есть рыжий кот Барсик"]
    both = [f["fact"] for f in await backend.search("У Ани есть кот", 5, "glados", groups=["glados", "other"])]
    assert len(both) == 2


async def test_groups_with_entities_finds_foreign_areas_of_the_same_character(backend):
    await backend.add_fact(Fact("Бурый", "КОЛОНИСТ", "Ферма", "Бурый колонист фермы"), T0, "glados:rimworld")
    await backend.add_fact(Fact("Бурый", "КОЛОНИСТ", "Ферма", "Бурый другого персонажа"), T0, "tester:rimworld")
    assert await backend.groups_with_entities("glados", ["бурый"]) == ["glados:rimworld"]
    assert await backend.groups_with_entities("glados", ["никто"]) == []


async def test_canon_is_protected_from_cleanup_and_retire(backend):
    uuid = await backend.add_fact(Fact("ГЛаДОС", "ПОМНИТ", "воспоминание m-1", "Я помню испытательную камеру"), T0, "glados")
    await backend.mark_memory("glados", uuid, "m-1", "episodic", "ГЛаДОС", 0.8, "seed")
    assert await backend.has_canon("glados", "ГЛаДОС", "ПОМНИТ")
    await backend.retire("ГЛаДОС", ("ПОМНИТ",), "glados")
    await backend.purge_retired(days=-1, group="glados")
    await backend.upsert_fact(Fact("ГЛаДОС", "ПОМНИТ", "x", "новое"), T0, "glados")
    assert (await backend.seed_rows("glados")) == {"m-1": (uuid, "Я помню испытательную камеру")}
    assert (await backend.self_memories("glados", "ГЛаДОС"))[0]["fact"] == "Я помню испытательную камеру"


async def test_colonist_upsert_replaces_retire_closes_and_purge_deletes(backend):
    await backend.upsert_fact(Fact("Борис", "СОСТОЯНИЕ", "ранен", "Борис ранен"), T0, "glados")
    await backend.upsert_fact(Fact("Борис", "СОСТОЯНИЕ", "здоров", "Борис здоров"), T0, "glados")
    assert backend.store.count("glados") == 1
    await backend.retire("Борис", COLONIST_RELATIONS, "glados")
    assert all(f["invalid_at"] for f in await backend.search("Борис", 5, "glados"))
    await backend.purge_retired(days=-1, group="glados")
    assert backend.store.count("glados") == 0 and await backend.search("Борис", 5, "glados") == []


async def test_delete_memory_and_delete_remove_from_search(backend):
    uuid = await backend.add_fact(Fact("Аня", "ХОББИ", "шахматы", "Аня играет в шахматы"), T0, "glados")
    await backend.delete(uuid, "glados")
    assert await backend.search("шахматы", 5, "glados") == []
    assert (await backend.snapshot(group="glados"))["edges"] == []


async def test_graph_memory_uses_backend_scores_without_extra_embedding(tmp_path, monkeypatch):
    import settings
    settings.init(str(tmp_path))
    b = SqliteBackend(tmp_path / "graph.db", embedder=HashEmbedder())
    await b.add_fact(Fact("Аня", "ВЛАДЕЕТ", "кот", "У Ани есть рыжий кот Барсик"), T0, "glados")
    g = gm.GraphMemory(str(tmp_path), backend=b)
    g._healthy, g._health_at = True, float("inf")
    found = await g.search("рыжий кот Барсик")
    assert [f["fact"] for f in found] == ["У Ани есть рыжий кот Барсик"] and found[0]["score"] > 0.5


def test_store_import_row_is_idempotent():
    s = FactStore()
    row = {"uuid": "u1", "group_id": "glados", "subject": "Аня", "relation": "ДРУГ", "object": "Вася", "fact": "Вася друг Ани",
           "valid_at": 1.0, "invalid_at": None, "canon": True, "source": "seed", "mem_id": "m-1", "mtype": "x", "about": "Аня",
           "weight": 0.5}
    assert s.import_row(row) is True and s.import_row(row) is False
    assert s.has_canon("glados", "аня", "ДРУГ")


async def test_migration_from_falkordb_rows_is_idempotent_and_searchable(tmp_path):
    from tools import migrate_falkordb as mig
    raw = ["Аня", "Казань", "u-1", "ЖИВЁТ_В", "Аня живёт в Казани", "glados", "glados", "2026-09-01T10:00:00+00:00", None,
           "2026-09-01T10:00:00+00:00", None, None, None, None, None, None]
    row = mig.row_of(raw, "glados")
    assert row["valid_at"] > 0 and row["invalid_at"] is None and row["group_id"] == "glados"
    assert mig.row_of(["Аня", "", "u-2", "X", "f"] + [None] * 11, "glados") is None
    b = SqliteBackend(tmp_path / "graph.db", embedder=HashEmbedder())
    assert await mig.migrate([row], b) == {"imported": 1, "skipped": 0, "embedded": 1}
    assert await mig.migrate([row], b) == {"imported": 0, "skipped": 1, "embedded": 0}
    assert [f["fact"] for f in await b.search("Аня живёт в Казани", 3, "glados")] == ["Аня живёт в Казани"]


async def test_stats_snapshot_and_explain_for_the_dashboard(tmp_path):
    import settings
    settings.init(str(tmp_path))
    b = SqliteBackend(tmp_path / "graph.db", embedder=HashEmbedder())
    await b.add_fact(Fact("Аня", "ЖИВЁТ_В", "Москва", "Аня живёт в Москве"), T0, "glados")
    await b.add_fact(Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня переехала в Казань и живёт в Казани"), T0 + timedelta(days=1), "glados")
    await b.add_fact(Fact("Бурый", "КОЛОНИСТ", "Ферма", "Бурый колонист фермы"), T0, "glados:rimworld")
    uuid = await b.add_fact(Fact("ГЛаДОС", "ПОМНИТ", "m-1", "Я помню камеру"), T0, "glados")
    await b.mark_memory("glados", uuid, "m-1", "episodic", "ГЛаДОС", 0.8, "seed")
    st = await b.stats("glados")
    assert (st["total"], st["active"], st["outdated"], st["canon"]) == (4, 3, 1, 1)
    assert st["areas"]["glados:rimworld"] == {"active": 1, "outdated": 0} and st["sources"] == {"extract": 3, "seed": 1}
    assert st["db_bytes"] > 0 and st["indexed"] == 4
    edges = (await b.snapshot(group="glados"))["edges"]
    assert {e["group"] for e in edges} == {"glados", "glados:rimworld"} and any(e["canon"] for e in edges)
    g = gm.GraphMemory(str(tmp_path), backend=b)
    g._healthy, g._health_at = True, float("inf")
    full = await g.stats()
    assert full["group"] == "glados" and full["queue"] == 0 and full["active"] == 3 and full["min_score"] == gm.MIN_SCORE
    out = await g.explain("Аня переехала в Казань и живёт в Казани")
    assert out["hits"][0]["fact"].startswith("Аня переехала") and "Аня переехала" in out["context"]
