"""Память-граф без сети и базы: разбор ответа модели, очередь в файле, повторы и отбраковка, поиск с порогом."""
import asyncio
import json

import pytest

import graph_memory as gm
import settings
from graph_memory import Fact, GraphMemory, parse_facts


def test_parse_facts_keeps_good_and_drops_bad():
    raw = json.dumps({"facts": [
        {"subject": "Аня", "relation": "живёт в", "object": "Казань", "fact": "Аня живёт в Казани"},
        {"subject": "Аня", "relation": "", "object": "x", "fact": "пустая связь"},
        {"subject": "Аня", "relation": "это", "object": "аня", "fact": "сама с собой"},
        {"subject": "A" * 90, "relation": "r", "object": "o", "fact": "слишком длинное имя"},
        {"subject": "Вася", "relation": "подруга"},
    ]})
    facts = parse_facts(raw)
    assert [(f.subject, f.relation, f.object) for f in facts] == [("Аня", "ЖИВЁТ_В", "Казань")]
    assert parse_facts("не json") == [] and parse_facts('{"facts": 5}') == []


class FakeExtractor:
    def __init__(self, facts=None, fail=0):
        self.calls, self._facts, self._fail = [], facts or [], fail

    async def extract(self, speaker, text):
        self.calls.append((speaker, text))
        if self._fail:
            self._fail -= 1
            raise RuntimeError("модель недоступна")
        return self._facts


class FakeBackend:
    def __init__(self, ok=True, found=None, scores=None):
        self.ok, self.added, self.found, self._scores = ok, [], found or [], scores or {}

    async def healthy(self):
        return (True, "работает") if self.ok else (False, "база не отвечает")

    async def add_fact(self, fact, when, group=None, **kw):
        self.added.append(fact)

    async def search(self, query, limit, group=None):
        return self.found

    async def scores(self, query, facts):
        return [self._scores.get(f, 0.0) for f in facts]       # оценка по тексту факта, как у настоящей модели

    async def delete(self, uuid, group=None):
        self.deleted = uuid


@pytest.fixture
def mem(tmp_path, monkeypatch):
    settings.init(str(tmp_path))
    return tmp_path


def make(mem, **kw):
    return GraphMemory(str(mem), backend=kw.pop("backend", FakeBackend()), extractor=kw.pop("extractor", FakeExtractor()), **kw)


def test_short_replies_are_not_queued_and_queue_survives_restart(mem):
    g = make(mem)
    g.enqueue("Аня", "ок")
    assert g.pending() == 0
    g.enqueue("Аня", "я переехала в Казань, работаю удалённо")
    assert g.pending() == 1
    assert make(mem).pending() == 1                      # очередь лежит в файле


def test_step_extracts_and_stores_facts(mem):
    f = Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня живёт в Казани")
    g = make(mem, extractor=FakeExtractor([f]))
    g.enqueue("Аня", "я переехала в Казань, работаю удалённо")
    assert asyncio.run(g.step()) is True
    assert g.backend.added == [f] and g.pending() == 0


def test_busy_conversation_and_dead_backend_hold_the_queue(mem):
    g = make(mem, busy=lambda: True)
    g.enqueue("Аня", "я переехала в Казань, работаю удалённо")
    assert asyncio.run(g.step()) is False and g.pending() == 1
    g2 = make(mem, backend=FakeBackend(ok=False))
    assert asyncio.run(g2.step()) is False and g2.pending() == 1
    assert "база не отвечает" in g2.status_text()


def test_failure_retries_then_drops(mem):
    g = make(mem, extractor=FakeExtractor(fail=10))
    g.enqueue("Аня", "я переехала в Казань, работаю удалённо")
    for _ in range(gm.MAX_ATTEMPTS):
        g._health_at = 0
        asyncio.run(g.step())
    assert g.pending() == 0


def test_disabled_in_settings(mem):
    settings.update(graph_memory=False)
    g = make(mem)
    g.enqueue("Аня", "я переехала в Казань, работаю удалённо")
    assert asyncio.run(g.step()) is False and "выключена" in g.status_text()


def test_search_drops_outdated_and_weak_matches(mem):
    found = [{"uuid": "1", "fact": "Аня живёт в Москве", "invalid_at": "2026-09-15"},
             {"uuid": "2", "fact": "Аня живёт в Казани", "invalid_at": None},
             {"uuid": "3", "fact": "Колонист получил ранение", "invalid_at": None}]
    g = make(mem, backend=FakeBackend(found=found, scores={"Аня живёт в Москве": 0.8, "Аня живёт в Казани": 0.7,
                                                              "Колонист получил ранение": 0.2}))
    g._healthy, g._health_at = True, 1e18
    res = asyncio.run(g.search("Где живёт Аня?"))
    assert [r["fact"] for r in res] == ["Аня живёт в Казани"]
    old = asyncio.run(g.search("Где жила Аня раньше?", include_old=True))
    assert [r["fact"] for r in old][:2] == ["Аня живёт в Москве", "Аня живёт в Казани"]


def test_context_block_and_forget(mem):
    found = [{"uuid": "2", "fact": "Аня живёт в Казани", "invalid_at": None}]
    g = make(mem, backend=FakeBackend(found=found, scores={"Аня живёт в Казани": 0.9}))
    g._healthy, g._health_at = True, 1e18
    block = asyncio.run(g.context_block("где живёт Аня"))
    assert "Аня живёт в Казани" in block
    assert asyncio.run(g.forget("2")) is True and g.backend.deleted == "2"


def test_minor_facts_are_not_stored_and_owner_needs_importance_four(mem):
    minor = Fact("Аня", "ЛЮБИТ", "чай", "Аня любит чай", 2)
    owner_mid = Fact("Аня", "УВЛЕКАЕТСЯ", "йога", "Аня увлекается йогой", 3)
    other_mid = Fact("Вася", "УВЛЕКАЕТСЯ", "шахматы", "Вася увлекается шахматами", 3)
    key = Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня живёт в Казани", 5)
    g = make(mem, extractor=FakeExtractor([minor, owner_mid, other_mid, key]))
    g.enqueue("Аня", "я переехала в Казань и теперь занимаюсь йогой", owner=True)
    assert asyncio.run(g.step()) is True
    assert [f.fact for f in g.backend.added] == ["Вася увлекается шахматами", "Аня живёт в Казани"]


# ── колонисты из игры ──

PAWN = {"name": "Boone", "age": 15, "skills": {"Animals": 4, "Plants": 3, "Intellectual": 3, "Melee": 0},
        "traits": ["Kind"], "health_pct": 1.0, "mood_pct": 0.5, "current_job": "LayDown"}


class ColonyBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.upserts, self.retired = [], []

    async def upsert_fact(self, fact, when, group=None):
        self.upserts.append(fact)

    async def retire(self, subject, relations, group=None):
        self.retired.append(subject)


def test_colonist_facts_come_from_game_data():
    facts = {f.relation: f.fact for f in gm.colonist_facts(PAWN, "Поселение")}
    assert set(facts) == {"КОЛОНИСТ", "НАВЫКИ", "ЧЕРТЫ", "СОСТОЯНИЕ"}
    links = {(f.relation, f.object) for f in gm.colonist_facts(PAWN, "Поселение")}
    assert ("КОЛОНИСТ", "Поселение") in links                     # колонист связан с узлом поселения
    cf = gm.colony_facts("Поселение", ["Boone", "Fell"])
    assert cf[0].subject == "ГЛаДОС" and cf[0].object == "Поселение" and "колонистов: 2 (Boone, Fell)" in cf[1].fact
    assert "Животные 4" in facts["НАВЫКИ"] and "Ближний бой" not in facts["НАВЫКИ"]
    assert "здоровье 100%" in facts["СОСТОЯНИЕ"] and "LayDown" in facts["СОСТОЯНИЕ"]


def test_colonists_overwrite_only_changes_and_retire_missing(mem):
    b = ColonyBackend()
    g = make(mem, backend=b)
    g._healthy, g._health_at = True, 1e18
    assert asyncio.run(g.sync_colonists([PAWN, dict(PAWN, name="Fell")], "Поселение")) == 10        # 2×4 + «управляет» + «состав»
    assert asyncio.run(g.sync_colonists([PAWN, dict(PAWN, name="Fell")], "Поселение", force=True)) == 0     # ничего не изменилось
    b.upserts.clear()
    hurt = dict(PAWN, health_pct=0.4, is_downed=True)
    assert asyncio.run(g.sync_colonists([hurt, dict(PAWN, name="Fell")], "Поселение", force=True)) == 1
    assert [u.relation for u in b.upserts] == ["СОСТОЯНИЕ"] and "лежит раненым" in b.upserts[0].fact  # перезаписано состояние
    asyncio.run(g.sync_colonists([hurt], "Поселение", force=True))                                     # Fell выбыл
    assert b.retired == ["Fell"]
