"""Единая память эпизодов: канон из memories.yaml, заметки «запомни», жизнь; поиск по словам без эмбеддера; блоки контекста;
защита канона (CHR-17); перенос старого graph.db; инструменты recall, remember, forget и пиры.

Модель и LM Studio не нужны: эмбеддер это заглушка или его нет вовсе."""
import json
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.character import character
from services.character import character_memory
from services.memory_service import character_seed
from services.memory_service import episodes as dom
from services.memory_service.adapters import UserMemoryAdapter
from services.memory_service.episode_memory import EpisodeMemory
from services.memory_service.episode_recall import EpisodeRecall
from services.memory_service.episode_store import EpisodeStore, canon_id
from services.memory_service.graph_import import MARK, migrate_graph, self_sufficient
from services.memory_service.search import HybridIndex, HybridSearch, SearchConfig

REAL_CHARACTERS = Path(__file__).resolve().parents[1] / "resources"
NOW = 1_800_000_000.0


def build(tmp_path, embedder=None):
    path = tmp_path / "episodes.db"
    store, index = EpisodeStore(path), HybridIndex(path)
    search = HybridSearch(index, embedder, None, SearchConfig(min_cos=0.5, min_cos_bm25=0.4))
    recall = EpisodeRecall(store=store, search=search)
    memory = EpisodeMemory(store=store, recall=recall, search=search)
    return SimpleNamespace(path=path, store=store, index=index, search=search, recall=recall, memory=memory)


def canon(w, mem_id, text, mtype="biography", weight=0.8):
    w.store.upsert_canon(mem_id, text, mtype=mtype, weight=weight)
    w.index.upsert("episodes", canon_id(mem_id), text, ts=NOW, meta={"episode": canon_id(mem_id), "kind": dom.CANON, "salience": weight})


def life(w, ep_id, title, text, salience=0.6, ts=NOW):
    w.store.add_episode({"id": ep_id, "t_start": ts, "t_end": ts, "first_id": 1, "last_id": 2, "salience": salience,
                         "status": "pending", "day": "2026-10-01"}, [])
    w.store.update(ep_id, title=title, text=text, status="summarized")
    w.index.upsert("episodes", ep_id, dom.index_text(title, text), ts=ts, meta={"episode": ep_id, "kind": dom.LIFE, "salience": salience})


class VecEmbedder:
    """Заглушка: вектор по точному тексту, остальное нейтрально."""

    def __init__(self, vecs, down=False):
        self.vecs, self.down = vecs, down

    async def embed(self, texts, query=False):
        if self.down:
            raise ConnectionError("LM Studio недоступен")
        return [self.vecs.get(t, [0.0, 0.0, 1.0]) for t in texts]


def _copy_characters(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "characters"
    shutil.copytree(REAL_CHARACTERS / "GlaDOS", root / "GlaDOS")
    monkeypatch.setattr(character, "CHARACTERS_DIR", root)
    return root


# ── эпизод это текст: находится по слову из текста и из заголовка без эмбеддера ──────────────────────────────

async def test_episode_is_found_by_a_word_from_its_text_and_from_its_title_without_embedder(tmp_path):
    w = build(tmp_path)
    life(w, "m1-2", "Экзамен по физике", "Аня сказала, что в пятницу у неё экзамен. Я записала Аню в календарь.")
    life(w, "m3-4", "Прогулка", "Мы с Борисом ходили к озеру и кормили уток.")
    by_text = await w.recall.find("календарь")
    by_title = await w.recall.find("физике")
    assert [h["id"] for h in by_text] == ["m1-2"] and [h["id"] for h in by_title] == ["m1-2"]
    assert by_text[0]["kind"] == "episode" and by_text[0]["text"].startswith("Экзамен по физике.")      # индексируется `заголовок. текст`
    assert [h["id"] for h in await w.recall.find("уток")] == ["m3-4"]


def test_index_text_is_title_and_text_and_nothing_else():
    assert dom.index_text("Заголовок.", "Текст.") == "Заголовок. Текст." and dom.index_text("", " Только текст ") == "Только текст"


# ── канон: загрузка из yaml, идемпотентность, защита ─────────────────────────────────────────────────────

def test_seed_adds_then_updates_by_id_without_duplicates_and_removes(tmp_path, monkeypatch):
    """CHR-16: загрузка по memories.yaml: новые добавлены, повторная не дублирует, изменённая заменена, удалённая убрана."""
    root = _copy_characters(tmp_path, monkeypatch)
    ch = character.load("glados")
    w = build(tmp_path)
    n = len(character_memory.load_memories(ch.path(character.MEMORIES_FILE)))
    assert character_seed.seed_canon(ch, w.store, w.index) == {"added": n, "updated": 0, "same": 0, "removed": 0}
    rows = w.store.of_kind(dom.CANON)
    assert len(rows) == n and all(r["mem_id"] and r["mtype"] and r["status"] == "summarized" for r in rows)
    assert w.index.count("episodes") == n
    assert character_seed.seed_canon(ch, w.store, w.index) == {"added": 0, "updated": 0, "same": n, "removed": 0}

    mem = root / "GlaDOS" / "core" / "memories.yaml"
    text = mem.read_text(encoding="utf-8").replace("хотя понимаю, что это шутка.", "и не объясняю почему.")
    mem.write_text(text, encoding="utf-8")
    edited = character_seed.seed_canon(character.load("glados"), w.store, w.index)
    assert edited["same"] == n - 1 and edited["updated"] == 1 and w.index.count("episodes") == n
    assert any("и не объясняю почему." in r["text"] and r["version"] == 1 for r in w.store.of_kind(dom.CANON))

    lines = text.splitlines()
    cut = next(i for i, line in enumerate(lines) if line.startswith(f"- id: m-{n:04d}"))
    mem.write_text("\n".join(lines[:cut]) + "\n", encoding="utf-8")
    removed = character_seed.seed_canon(character.load("glados"), w.store, w.index)
    assert removed["removed"] == 1 and len(w.store.of_kind(dom.CANON)) == n - 1 and w.index.count("episodes") == n - 1


def test_seed_works_without_any_embedder_and_words_find_it(tmp_path):
    w = build(tmp_path)
    character_seed.seed_canon(character.load("glados"), w.store, w.index)
    assert w.index.search("Кэролайн", None, collections=["episodes"], k=5)


def test_canon_is_protected_from_forgetting_and_from_being_overwritten(tmp_path):
    w = build(tmp_path)
    canon(w, "m-11", "Когда-то я была человеком, и меня звали Кэролайн.")
    cid = canon_id("m-11")
    assert w.memory.forget_matching("человеком Кэролайн") == 0 and w.memory.forget_matching("Кэролайн", strict=True) == 0
    assert w.memory.forget(cid) is False and w.store.delete(cid) is False
    assert w.memory.remember("Когда-то я была человеком, и меня звали Кэролайн.", 5) is True       # заметка рядом, не поверх
    assert w.store.get(cid)["kind"] == dom.CANON and w.store.get(cid)["text"].startswith("Когда-то")
    assert len(w.store.of_kind(dom.CANON)) == 1 and len(w.store.of_kind(dom.NOTE)) == 1


def test_canon_changes_only_through_the_seed_methods(tmp_path):
    w = build(tmp_path)
    canon(w, "m-1", "Первый текст про лабораторию.")
    assert w.store.upsert_canon("m-1", "Первый текст про лабораторию.", mtype="biography", weight=0.8) == "same"
    assert w.store.upsert_canon("m-1", "Другой текст.", mtype="opinion", weight=0.5) == "updated"
    assert w.store.get(canon_id("m-1"))["mtype"] == "opinion" and w.store.remove_canon("m-1") and not w.store.of_kind(dom.CANON)


# ── блок «Твои воспоминания (это о тебе)» ────────────────────────────────────────────────────────────────

async def test_canon_found_by_words_when_the_embedder_is_down(tmp_path):
    """LM Studio выключен: воспоминание с именем из реплики всё равно находится (иначе модель выдумывает из своих знаний)."""
    w = build(tmp_path, VecEmbedder({}, down=True))
    canon(w, "m-11", "Когда-то я была человеком, и меня звали Кэролайн.")
    canon(w, "m-12", "Вспоминать Кэролайн мне больно и неприятно.", "taboo")
    canon(w, "m-3", "Люди ошибаются в одних и тех же местах.", "opinion", 0.75)
    block = await w.memory.canon_block("помнишь Кэролайн?")
    assert block.startswith("Твои воспоминания (это о тебе):") and "звали Кэролайн" in block and "больно" in block and "Люди" not in block
    assert await w.memory.canon_block("расскажи про погоду") == ""


async def test_canon_found_by_words_without_an_embedder_object_at_all(tmp_path):
    w = build(tmp_path, None)
    canon(w, "m-11", "Когда-то я была человеком, и меня звали Кэролайн.")
    assert "Кэролайн" in await w.memory.canon_block("помнишь Кэролайн?")


async def test_canon_follows_cosine_threshold_when_vectors_exist(tmp_path):
    fact = "Когда-то я была человеком."
    w = build(tmp_path, VecEmbedder({fact: [1.0, 0.0, 0.0], "ты помнишь о своей прошлой жизни?": [0.45, 0.9, 0.0],
                                     "какая погода": [0.2, 0.0, 0.98]}))
    w.store.upsert_canon("m-11", fact, mtype="biography", weight=0.7)
    await w.search.add("episodes", canon_id("m-11"), fact, meta={"episode": canon_id("m-11"), "kind": dom.CANON, "salience": 0.7})
    assert "Когда-то я была человеком" in await w.memory.canon_block("ты помнишь о своей прошлой жизни?")    # косинус ~0,45 выше порога канона
    assert await w.memory.canon_block("какая погода") == ""


async def test_canon_nudge_every_fifth_quiet_reply_then_counter_restarts(tmp_path):
    """CHR-19: при пустом отборе раз в пять реплик напоминание о привычке, потом счётчик сбрасывается."""
    w = build(tmp_path)
    canon(w, "m-6", "Люблю паузы подытоживать.", "habit", 0.6)
    got = [await w.memory.canon_block("совсем другая тема про погоду") for _ in range(5)]
    assert got[:4] == ["", "", "", ""] and "паузы подытоживать" in got[4]
    assert await w.memory.canon_block("ещё погода") == "", "после напоминания счётчик начинается заново"


async def test_canon_block_gives_at_most_three_by_weight(tmp_path):
    w = build(tmp_path)
    for i in range(5):
        canon(w, f"m-{i}", f"Про лабораторию номер {i} я помню многое.", "biography", 0.5 + i / 10)
    block = await w.memory.canon_block("расскажи про лабораторию")
    assert len(block.splitlines()) == 1 + character_memory.N_PICK and "номер 4" in block and "номер 0" not in block


# ── заметки и блок «Что ты помнишь по теме» ──────────────────────────────────────────────────────────────

async def test_remember_writes_notes_takes_only_the_important_and_finds_them_by_topic(tmp_path):
    w = build(tmp_path)
    m = w.memory
    assert m.remember("Пользователь любит зелёный чай по утрам", 4) is True
    assert m.remember("Пользователь сегодня устал", 2) is False                                   # мелочь не пишется
    assert m.remember("Просит меня шутить реже", 5) is True                                       # пожелание к ней тоже заметка
    assert m.remember("Пользователь любит зелёный чай по утрам", 4) is False                      # дубль
    notes = w.store.of_kind(dom.NOTE)
    assert sorted(n["salience"] for n in notes) == [0.8, 1.0] and all(n["status"] == "summarized" and n["title"] == "" for n in notes)
    block = await m.topic_block("а ты помнишь про зелёный чай?")
    assert block.startswith("Что ты помнишь по теме") and "зелёный чай" in block and "шутить" not in block


async def test_notes_about_the_owner_are_ordinary_episodes_and_never_pinned(tmp_path):
    w = build(tmp_path)
    w.memory.remember("Пользователь любит зелёный чай по утрам", 5)
    assert await w.memory.topic_block("погода в Казани") == ""                                    # вне темы не лезет в контекст
    assert not hasattr(w.memory, "profile_block") and not hasattr(w.store, "profile_facts")
    assert "pinned" not in {r[1] for r in w.store._db.execute("PRAGMA table_info(episodes)")}


async def test_topic_block_has_life_and_notes_but_not_canon_and_weights_by_salience(tmp_path):
    w = build(tmp_path)
    canon(w, "m-1", "Люблю тишину в лаборатории.")
    life(w, "e1", "Тишина", "Мы с Аней долго сидели в тишине и пили чай.", salience=0.9)
    life(w, "e2", "Другая тишина", "Тишина в коридоре была слишком долгой, я это отметила.", salience=0.1)
    w.memory.remember("Аня попросила тишины по утрам", 4)
    block = await w.memory.topic_block("что с тишиной?")
    assert "Люблю тишину" not in block and "Аней долго" in block and "утрам" in block
    hits = await w.recall.find("тишина", kinds=(dom.LIFE,), diary=False)
    assert [h["id"] for h in hits] == ["e1", "e2"]                                                 # яркий выше тусклого


async def test_recent_and_bright_wins_at_equal_text_match(tmp_path):
    w = build(tmp_path)
    life(w, "old", "Чай", "Пили чай в лаборатории.", salience=0.2)
    life(w, "new", "Чай", "Пили чай в лаборатории.", salience=0.9)
    assert [h["id"] for h in await w.recall.find("чай")][0] == "new"


# ── забывание ────────────────────────────────────────────────────────────────────────────────────────────

def test_forget_matching_removes_notes_and_life_but_not_canon(tmp_path):
    w = build(tmp_path)
    canon(w, "m-1", "Я люблю чай с лимоном.")
    w.memory.remember("Пользователь любит зелёный чай по утрам", 4)
    w.memory.remember("Пользователь работает дизайнером", 4)
    life(w, "e1", "Чай", "Аня пила зелёный чай и рассказывала про отпуск.")
    assert w.memory.forget_matching("забудь про зелёный чай") == 2                        # заметка и эпизод, канон на месте
    assert [n["text"] for n in w.store.of_kind(dom.NOTE)] == ["Пользователь работает дизайнером"]
    assert w.store.get(canon_id("m-1")) and w.store.get("e1") is None and w.index.search("отпуск", None) == []
    assert w.memory.forget_matching("дизайнером", strict=True) == 1 and w.store.of_kind(dom.NOTE) == []


# ── адаптер: куда что пишется ────────────────────────────────────────────────────────────────────────────

class _Tasks:
    def __init__(self):
        self.plans = []

    async def upsert_plan(self, plan_id, text, due):
        self.plans.append((plan_id, text, due))


async def test_adapter_routes_user_self_episode_to_notes_and_plans_to_tasks(tmp_path):
    import asyncio
    w, tasks = build(tmp_path), _Tasks()
    ad = UserMemoryAdapter(lambda: None, lambda: SimpleNamespace(memory=w.memory, recall=w.recall), lambda: tasks)
    ad.loop = asyncio.get_running_loop()
    assert ad.add_fact("Любимый цвет пользователя зелёный", "user", 4) is True
    assert ad.add_fact("Просит не язвить по утрам", "self", 4) is True
    assert ad.add_fact("Событие, которое она сама сочла важным", "episode", 5) is True
    assert ad.add_fact("Пустяк про погоду за окном", "user", 2) is False                          # ниже порога
    assert ad.add_fact("Что-то неизвестного вида", "whatever", 5) is False
    assert ad.add_fact("Завтра экзамен по физике", "plan", 3) is True
    assert await asyncio.to_thread(ad.add_fact, "Сдать отчёт в пятницу", "plan", 4, 3) is True    # из рабочего потока
    await asyncio.sleep(0.05)
    assert len(w.store.of_kind(dom.NOTE)) == 3 and [p[1] for p in tasks.plans] == ["Завтра экзамен по физике", "Сдать отчёт в пятницу"]
    assert tasks.plans[0][0].startswith("m-") and tasks.plans[0][2] > time.time()


async def test_adapter_without_episodes_or_tasks_stores_and_returns_nothing():
    ad = UserMemoryAdapter(lambda: None)
    assert ad.add_fact("Любит чай и шоколад", "user", 5) is False and ad.add_fact("Экзамен завтра утром", "plan", 5) is False
    assert await ad.topic_block("чай") == "" and await ad.canon_block("чай") == "" and await ad.recall("чай") == []
    assert ad.forget_matching("чай") == 0 and ad.memory_status() is None
    assert not hasattr(ad, "facts") and not hasattr(ad, "profile_block") and not hasattr(ad, "enqueue_for_graph")


async def test_adapter_blocks_recall_and_world_state(tmp_path):
    w = build(tmp_path)
    canon(w, "m-11", "Когда-то я была человеком, и меня звали Кэролайн.")
    w.memory.remember("Пользователь любит зелёный чай по утрам", 4)
    world = SimpleNamespace(block=lambda q: "Состояние игры: Борис в порядке." if "Борис" in q else "")
    ad = UserMemoryAdapter(lambda: None, lambda: SimpleNamespace(memory=w.memory, recall=w.recall), None, lambda: world)
    assert "Кэролайн" in await ad.canon_block("помнишь Кэролайн?")
    assert "зелёный чай" in await ad.topic_block("зелёный чай")
    assert "Борис в порядке" in await ad.topic_block("как там Борис?")
    got = await ad.recall("Кэролайн")
    assert got and got[0]["kind"] == "canon" and "Кэролайн" in got[0]["text"] and 1 <= got[0]["importance"] <= 5
    assert {h["kind"] for h in await ad.recall("зелёный чай")} == {"note"}
    assert ad.memory_status() == "Память: эпизодов 0, заметок 1, канон персонажа 1."


# ── инструменты ядра ─────────────────────────────────────────────────────────────────────────────────────

def _tools(companion):
    from environments import builtin

    class Registry:
        def __init__(self):
            self.envs = {}

        def add(self, env):
            self.envs[env.id] = env
    reg = Registry()
    builtin.register(reg, companion, None)
    return {t.name: t for t in reg.envs["assistant"].tools}


async def test_recall_forget_and_remember_tools_work_on_episodes(tmp_path):
    from environments.base import ToolError
    w = build(tmp_path)
    canon(w, "m-11", "Когда-то я была человеком, и меня звали Кэролайн.")
    life(w, "m1-2", "Озеро", "Мы с Аней ходили на озеро, кормили уток.")
    eps = SimpleNamespace(memory=w.memory, recall=w.recall)
    writer = UserMemoryAdapter(lambda: None, lambda: eps)

    async def remember_text(text, kind=None):
        return [text] if writer.add_fact(text, kind or "user", 4) else []

    comp = SimpleNamespace(episodes=eps, memory_writer=writer, remember_text=remember_text)
    tools = _tools(comp)
    assert "эпизод m1-2" in await tools["recall"].call({"query": "озеро"})
    assert "о себе" in await tools["recall"].call({"query": "Кэролайн"})
    with pytest.raises(ToolError):
        await tools["recall"].call({"query": "несуществующее"})
    assert "запомнила" in await tools["remember"].call({"text": "Аня любит зелёный чай", "kind": "user"})
    assert "зелёный чай" in await tools["recall"].call({"query": "чай"})
    assert "стёрто записей из памяти: 1" in await tools["forget"].call({"text": "зелёный чай"})
    with pytest.raises(ToolError):
        await tools["forget"].call({"text": "Кэролайн человеком"})                                # канон не стирается
    assert w.store.get(canon_id("m-11"))


async def test_peer_recall_and_remember_work_on_episodes(tmp_path):
    from services.peers.requests import CoreRequests
    w = build(tmp_path)
    life(w, "m1-2", "Озеро", "Мы с Аней ходили на озеро, кормили уток.", salience=0.8)
    eps = SimpleNamespace(memory=w.memory, recall=w.recall)
    ad = UserMemoryAdapter(lambda: None, lambda: eps)
    req = CoreRequests(memory_reader=ad, memory_writer=ad)
    assert await req.handle(None, "core.memory.remember", {"text": " любит чай "}) == {"stored": True}
    assert await req.handle(None, "core.memory.remember", {"text": "любит чай"}) == {"stored": False}
    got = (await req.handle(None, "core.memory.recall", {"query": "чай", "limit": 3}))["facts"]
    assert got == [{"text": "любит чай", "kind": "note", "importance": 4}]
    lake = (await req.handle(None, "core.memory.recall", {"query": "озеро"}))["facts"]
    assert lake[0]["kind"] == "episode" and lake[0]["importance"] == 4


# ── перенос старого graph.db ─────────────────────────────────────────────────────────────────────────────

GRAPH_SCHEMA = (
    "CREATE TABLE facts (uuid TEXT PRIMARY KEY, group_id TEXT NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, "
    "object TEXT NOT NULL, fact TEXT NOT NULL, subj_key TEXT NOT NULL, obj_key TEXT NOT NULL, valid_at REAL, invalid_at REAL, "
    "created_at REAL NOT NULL, canon INTEGER NOT NULL DEFAULT 0, source TEXT, mem_id TEXT, mtype TEXT, about TEXT, "
    "weight REAL NOT NULL DEFAULT 0, importance INTEGER NOT NULL DEFAULT 3, profile TEXT)")


def make_graph(folder: Path, with_vectors=True, schema=GRAPH_SCHEMA):
    path = folder / "graph.db"
    db = sqlite3.connect(path)
    db.execute(schema)
    cols = ("uuid", "group_id", "subject", "relation", "object", "fact", "subj_key", "obj_key", "valid_at", "invalid_at", "created_at",
            "canon", "source", "mem_id", "mtype", "about", "weight", "importance", "profile")
    rows = [
        ("c1", "glados", "ГЛаДОС", "ПОМНИТ", "воспоминание m-1", "Когда-то я была человеком, и меня звали Кэролайн.", "глаДОС", "x", NOW, None,
         NOW, 1, "seed", "m-1", "biography", "self", 0.8, 3, None),
        ("o1", "glados", "Аня", "ЗАМЕТКА", "x", "Аня любит зелёный цвет", "аня", "x", NOW, None, NOW, 0, "remember", None, None,
         None, 0.0, 5, "owner"),
        ("o2", "glados", "Аня", "ЗАМЕТКА", "x", "любит чай", "аня", "x", NOW, None, NOW + 1, 0, "remember", None, None, None, 0.0, 4, "owner"),
        ("f1", "glados", "Вася", "ПОДРУГА", "Аня", "Вася - подруга Ани", "вася", "аня", NOW, None, NOW + 2, 0, "extract", None, None, None,
         0.0, 3, None),
        ("w1", "glados", "Борис", "КОЛОНИСТ", "Дом", "Борис - колонист поселения «Дом» в RimWorld, 30 лет", "борис", "дом", NOW, None, NOW,
         0, "extract", None, None, None, 0.0, 3, None),
        ("w2", "glados", "Борис", "СОСТОЯНИЕ", "x", "Сейчас Борис: здоровье 90%", "борис", "x", NOW, None, NOW, 0, "extract", None, None, None,
         0.0, 3, None),
        ("old", "glados", "Аня", "ЖИВЁТ_В", "Москва", "Аня живёт в Москве", "аня", "москва", NOW - 99, NOW, NOW - 99, 0, "extract", None, None,
         None, 0.0, 4, None),
        ("alien", "tester", "Аня", "ЗАМЕТКА", "x", "Чужой персонаж помнит это", "аня", "x", NOW, None, NOW, 0, "remember", None, None, None,
         0.0, 4, "owner"),
    ]
    db.executemany(f"INSERT INTO facts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    db.commit()
    db.close()
    if with_vectors:                                              # старый индекс поиска в том же файле: коллекция = группа, ключ = uuid
        old = HybridIndex(path)
        old.upsert("glados", "o1", "Аня любит зелёный цвет", vec=[0.1, 0.2, 0.3], ts=NOW)
        old.upsert("glados", "o2", "любит чай", vec=[0.3, 0.2, 0.1], ts=NOW)
        old._db.close()
    return path


def test_graph_migrates_to_canon_and_notes_without_colonists_and_is_idempotent(tmp_path):
    path = make_graph(tmp_path)
    w = build(tmp_path)
    done = migrate_graph(tmp_path, w.store, w.index, "glados", clock=lambda: NOW)
    assert done == {"canon": 1, "notes": 3, "skipped": 3}
    assert w.store.get_meta(MARK)["notes"] == 3
    canons, notes = w.store.of_kind(dom.CANON), w.store.of_kind(dom.NOTE)
    assert [(c["mem_id"], c["mtype"], c["weight"], c["status"]) for c in canons] == [("m-1", "biography", 0.8, "summarized")]
    assert sorted(n["text"] for n in notes) == sorted(["Аня любит зелёный цвет", "Аня: любит чай", "Вася - подруга Ани"])
    assert {n["text"]: n["salience"] for n in notes}["Аня любит зелёный цвет"] == 1.0
    assert not path.exists() and (tmp_path / "graph.db.migrated").exists()                         # переименован, не удалён
    assert migrate_graph(tmp_path, w.store, w.index, "glados") is None                              # повторно ничего не делает
    assert len(w.store.of_kind(dom.NOTE)) == 3 and len(w.store.of_kind(dom.CANON)) == 1


async def test_migrated_notes_are_searchable_by_words_and_keep_vectors_when_the_text_is_unchanged(tmp_path):
    make_graph(tmp_path)
    w = build(tmp_path)
    migrate_graph(tmp_path, w.store, w.index, "glados", clock=lambda: NOW)
    assert [h["kind"] for h in await w.recall.find("зелёный")] == ["note"]
    assert "Кэролайн" in await w.memory.canon_block("помнишь Кэролайн?")
    notes = {n["text"]: n["id"] for n in w.store.of_kind(dom.NOTE)}
    vecs = w.index.vectors("episodes", list(notes.values()))
    assert list(vecs) == [notes["Аня любит зелёный цвет"]]                              # у «Аня: любит чай» текст изменён: вектор позже


def test_graph_migration_ignores_missing_or_broken_files_and_old_schemas(tmp_path):
    w = build(tmp_path)
    assert migrate_graph(tmp_path, w.store, w.index, "glados") is None and w.store.get_meta(MARK) is None      # файла нет: отметки нет
    (tmp_path / "graph.db").write_bytes(b"not a database")
    assert migrate_graph(tmp_path, w.store, w.index, "glados") is None and (tmp_path / "graph.db").exists()
    (tmp_path / "graph.db").unlink()
    old = GRAPH_SCHEMA.replace(", importance INTEGER NOT NULL DEFAULT 3, profile TEXT", "")           # старая база без importance и profile
    path = tmp_path / "graph.db"
    db = sqlite3.connect(path)
    db.execute(old)
    db.execute("INSERT INTO facts (uuid, group_id, subject, relation, object, fact, subj_key, obj_key, created_at) "
               "VALUES ('a', 'glados', 'Аня', 'ЛЮБИТ', 'чай', 'Аня любит чай', 'аня', 'чай', 1.0)")
    db.commit()
    db.close()
    assert migrate_graph(tmp_path, w.store, w.index, "glados", clock=lambda: NOW) == {"canon": 0, "notes": 1, "skipped": 0}
    assert w.store.of_kind(dom.NOTE)[0]["salience"] == 0.6


def test_self_sufficient_adds_the_subject_name_only_when_missing():
    assert self_sufficient("Аня", "любит чай") == "Аня: любит чай"
    assert self_sufficient("Аня", "Аня живёт в Казани") == "Аня живёт в Казани"
    assert self_sufficient("", "Любит чай") == "Любит чай"
    assert self_sufficient("Пользователь", "Пользователю нужно построить спальню") == "Пользователю нужно построить спальню"   # основа слова
    assert self_sufficient("Пользователь", "Построить комнату") == "Пользователь: Построить комнату"


# ── база эпизодов старой схемы (10d8d65 и раньше) ────────────────────────────────────────────────────────

OLD_EPISODES = """
CREATE TABLE episodes (
    id TEXT PRIMARY KEY, t_start REAL NOT NULL, t_end REAL NOT NULL, first_id INTEGER NOT NULL, last_id INTEGER NOT NULL,
    stream TEXT NOT NULL DEFAULT '', module TEXT NOT NULL DEFAULT '', participants TEXT NOT NULL DEFAULT '[]',
    peak_id INTEGER, salience REAL NOT NULL DEFAULT 0, emotions TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
    attitude TEXT NOT NULL DEFAULT '', noticed TEXT NOT NULL DEFAULT '[]',
    quotes TEXT NOT NULL DEFAULT '[]', facts TEXT NOT NULL DEFAULT '[]', related TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 0, character TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', facts_done INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0, compressed INTEGER NOT NULL DEFAULT 0, raw_deleted INTEGER NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL, updated REAL NOT NULL);
"""


def test_old_episode_database_is_upgraded_to_text_and_loses_the_separate_columns(tmp_path):
    path = tmp_path / "episodes.db"
    db = sqlite3.connect(path)
    db.executescript(OLD_EPISODES)
    db.execute("INSERT INTO episodes (id, t_start, t_end, first_id, last_id, title, summary, attitude, noticed, quotes, status, created, updated)"
               " VALUES ('m1-2', 1, 2, 1, 2, 'Озеро', 'Мы ходили на озеро.', 'Мне там спокойно.', ?, '[\"цитата\"]', 'embedded', 1, 1)",
               (json.dumps(["Аня молчала"], ensure_ascii=False),))
    db.commit()
    db.close()
    store = EpisodeStore(path)
    cols = {r[1] for r in store._db.execute("PRAGMA table_info(episodes)")}
    assert {"kind", "text", "mem_id", "mtype", "weight"} <= cols
    assert not cols & {"summary", "attitude", "noticed", "quotes", "facts", "emotions", "facts_done", "outcome"}
    ep = store.get("m1-2")
    assert ep["kind"] == dom.LIFE and ep["status"] == "embedded" and ep["title"] == "Озеро"
    assert ep["text"] == "Мы ходили на озеро. Аня молчала Мне там спокойно."
    store.close()
    again = EpisodeStore(path)                                                                          # повторное открытие безопасно
    assert again.get("m1-2")["text"].startswith("Мы ходили") and again.counts()["kinds"] == {"life": 1, "canon": 0, "note": 0}


def test_store_select_by_kind_and_listing_carries_the_kind(tmp_path):
    import asyncio
    w = build(tmp_path)
    canon(w, "m-1", "Я люблю тишину.")
    w.memory.remember("Аня просила писать короче", 4)
    life(w, "e1", "Чай", "Пили чай.")
    assert [e["kind"] for e in w.store.select(kind=dom.NOTE)] == [dom.NOTE] and len(w.store.select()) == 3
    kinds = {e["id"]: e["kind"] for e in asyncio.run(w.recall.listing())}
    assert sorted(kinds.values()) == ["canon", "life", "note"]
    assert w.store.counts()["pending"] == 0                                                               # канон и заметки не «ждут записи»


# ── состояние колонии (world state, не память) ───────────────────────────────────────────────────────────

def test_colony_state_is_rewritten_by_game_data_and_answers_by_topic(tmp_path):
    from services.event_handlers.colony_state import ColonyState
    cs = ColonyState(tmp_path / "colony_state.json")
    pawns = [{"name": "Борис", "age": 31, "skills": {"Shooting": 9, "Cooking": 2}, "traits": ["Ночная сова"], "health_pct": 0.9,
              "mood_pct": 0.6, "is_downed": True},
             {"name": "Вика", "age": 25}, {"name": "Пленный", "is_prisoner": True}]
    assert cs.sync(pawns, "Дом") is True and cs.sync(pawns, "Дом") is False
    assert cs.sync([], "Дом") is False and "Борис" in cs.pawns                                          # пустой список: игры нет, а не «все ушли»
    block = cs.block("как там Борис?")
    assert "Состояние игры" in block and "Стрельба 9" in block and "лежит раненым" in block and "Вика" not in block
    assert "колонистов: 2 (Борис, Вика)" in cs.block("что с колонией?") and cs.block("погода") == ""
    cs.sync([{"name": "Вика", "age": 25}], "Дом")                                                        # Борис выбыл: пропал сразу
    assert "Борис" not in cs.block("как Борис?") and ColonyState(tmp_path / "colony_state.json").pawns.keys() == {"Вика"}


# ── сон добавляет вектор заметкам и канону, записанным без эмбеддера ─────────────────────────────────────

async def test_sleep_embeds_notes_and_canon_written_without_a_vector(tmp_path):
    from test_episodes import Clock, Journal, LLM, T0, good_summary, make_runner
    r = make_runner(tmp_path, Journal(), LLM(good_summary), Clock(T0 + 86400))
    memory = EpisodeMemory(store=r.store, recall=EpisodeRecall(store=r.store, search=r.search), search=r.search)
    assert memory.remember("Пользователь любит зелёный чай по утрам", 4)
    r.store.upsert_canon("m-1", "Люблю тишину в лаборатории.", mtype="habit", weight=0.6)
    r.search.index.upsert("episodes", canon_id("m-1"), "Люблю тишину в лаборатории.", ts=T0, meta={"kind": dom.CANON, "salience": 0.6})
    ids = [n["id"] for n in r.store.of_kind(dom.NOTE)] + [canon_id("m-1")]
    assert r.search.index.vectors("episodes", ids) == {}                                    # слова есть, векторов пока нет
    rep = await r.runner.run(dom.SleepPlan("idle", None))
    assert rep["embedded"] == 2 and all(r.store.get(i)["status"] == "embedded" for i in ids)
    assert set(r.search.index.vectors("episodes", ids)) == set(ids)
    assert r.store.get(ids[0])["related"] == [] and r.store.get(ids[0])["notified"] == 1
