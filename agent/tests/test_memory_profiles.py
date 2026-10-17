"""Упрощённая память: профиль владельца и её профиль о себе в графе, планы как задачи, разовый перенос memories.json."""
import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from adapters.memory import UserMemoryAdapter
from domain import plan_due
from fact_store import FactStore
from graph_memory import GraphMemory
from memory_migration import BACKUP, FILE, migrate

NOW = 1_800_000_000.0


def _graph(tmp_path):
    return GraphMemory(str(tmp_path), busy=lambda: False)


# ── хранилище фактов ─────────────────────────────────────────────────────────

def test_store_keeps_importance_and_profile_and_raises_importance_on_repeat():
    st = FactStore()
    a, new = st.add("Аня", "ЗАМЕТКА", "x", "Аня любит чай", NOW, "g", now=NOW, importance=4, profile="owner")
    b, again = st.add("Аня", "ЗАМЕТКА", "x", "Аня любит чай", NOW, "g", now=NOW, importance=5)
    assert new and not again and a == b
    assert st.profile_facts("g", "owner")[0]["importance"] == 5
    st.add("Аня", "ЖИВЁТ_В", "Казань", "Аня живёт в Казани", NOW, "g", now=NOW, canon=True, profile="owner")
    assert [f["text"] for f in st.profile_facts("g", "owner")] == ["Аня любит чай"]        # канон в профиль не входит


def test_old_database_without_new_columns_is_upgraded(tmp_path):
    path = tmp_path / "graph.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE facts (uuid TEXT PRIMARY KEY, group_id TEXT NOT NULL, subject TEXT NOT NULL, relation TEXT NOT NULL, "
               "object TEXT NOT NULL, fact TEXT NOT NULL, subj_key TEXT NOT NULL, obj_key TEXT NOT NULL, valid_at REAL, invalid_at REAL, "
               "created_at REAL NOT NULL, canon INTEGER NOT NULL DEFAULT 0, source TEXT, mem_id TEXT, mtype TEXT, about TEXT, "
               "weight REAL NOT NULL DEFAULT 0)")
    db.commit()
    db.close()
    st = FactStore(path)
    st.add("Аня", "ЗАМЕТКА", "x", "Аня любит чай", NOW, "g", now=NOW, importance=4, profile="owner")
    assert st.profile_facts("g", "owner")[0]["importance"] == 4


# ── граф: запись, порог, чтение, забывание ────────────────────────────────────

def test_remember_takes_only_important_and_builds_profile_block(tmp_path):
    g = _graph(tmp_path)
    assert g.remember("Пользователь любит зелёный чай по утрам", "owner", 4, subject="Аня") is True
    assert g.remember("Пользователь сегодня устал", "owner", 2) is False                       # мелочь не пишется
    assert g.remember("Я пообещала шутить реже", "self", 5) is True
    assert g.remember("Пользователь любит зелёный чай по утрам", "owner", 4) is False           # дубль
    block = g.profile_block()
    assert "зелёный чай" in block and "шутить реже" in block and "устал" not in block
    assert [f["text"] for f in g.profile_facts("owner")] == ["Пользователь любит зелёный чай по утрам"]


def test_forget_matching_removes_profile_fact_by_words(tmp_path):
    g = _graph(tmp_path)
    g.remember("Пользователь любит зелёный чай по утрам", "owner", 4)
    g.remember("Пользователь работает дизайнером", "owner", 4)
    assert g.forget_matching("забудь что я люблю зелёный чай") == 1
    assert [f["text"] for f in g.profile_facts("owner")] == ["Пользователь работает дизайнером"]
    assert g.forget_matching("дизайнером", strict=True) == 1 and g.profile_facts("owner") == []


# ── адаптер: куда что пишется ─────────────────────────────────────────────────

class _Tasks:
    def __init__(self):
        self.plans = []

    async def upsert_plan(self, plan_id, text, due):
        self.plans.append((plan_id, text, due))


def test_adapter_routes_kinds_to_graph_and_tasks(tmp_path):
    g, tasks = _graph(tmp_path), _Tasks()
    ad = UserMemoryAdapter(lambda: None, lambda: g, None, lambda: tasks, lambda: "Аня")

    async def run():
        ad.loop = asyncio.get_running_loop()
        assert ad.add_fact("Любимый цвет пользователя зелёный", "user", 4) is True
        assert ad.add_fact("Обещала не язвить по утрам", "self", 4) is True
        assert ad.add_fact("Событие, которое она сама сочла важным", "episode", 5) is True
        assert ad.add_fact("Пустяк про погоду за окном", "user", 3) is False                    # ниже порога 4
        assert ad.add_fact("Завтра экзамен по физике", "plan", 3) is True
        assert await asyncio.to_thread(ad.add_fact, "Сдать отчёт в пятницу", "plan", 4, 3) is True    # из рабочего потока
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert {f["text"] for f in ad.facts()} == {"Любимый цвет пользователя зелёный"}
    assert len(g.profile_facts("self")) == 2
    texts = [p[1] for p in tasks.plans]
    assert texts == ["Завтра экзамен по физике", "Сдать отчёт в пятницу"]
    assert tasks.plans[0][0].startswith("m-") and tasks.plans[0][2] > time.time()


def test_adapter_without_graph_or_tasks_stores_nothing():
    ad = UserMemoryAdapter(lambda: None, lambda: None)
    assert ad.add_fact("Любит чай и шоколад", "user", 5) is False
    assert ad.add_fact("Экзамен завтра утром", "plan", 5) is False
    assert ad.facts() == [] and ad.profile_block() == "" and ad.forget_matching("чай") == 0


def test_plan_due_follows_words_in_the_text():
    ts = 1_800_000_000.0
    assert plan_due.due_of_text("экзамен завтра", ts) > ts
    assert plan_due.due_of_text("экзамен завтра", ts) < plan_due.due_of_text("экзамен послезавтра", ts)
    assert plan_due.due_of_text("когда-нибудь убраться", ts) == ts + plan_due.UNDATED_DAYS * 86400


# ── перенос старой памяти ─────────────────────────────────────────────────────

def _old_file(folder, now):
    items = [
        {"id": "a", "kind": "user", "text": "Любит зелёный чай по утрам", "importance": 5, "ts": now, "due": None},
        {"id": "b", "kind": "user", "text": "Видел забавную картинку сегодня", "importance": 3, "ts": now, "due": None},
        {"id": "c", "kind": "self", "text": "Я обещала шутить реже", "importance": 4, "ts": now, "due": None},
        {"id": "d", "kind": "episode", "text": "Мы вместе чинили колонию", "importance": 4, "ts": now, "due": None},
        {"id": "e", "kind": "plan", "text": "Сдать отчёт", "importance": 4, "ts": now, "due": now + 86400},
        {"id": "f", "kind": "plan", "text": "Давний просроченный план", "importance": 4, "ts": now - 90 * 86400, "due": now - 80 * 86400},
    ]
    (folder / FILE).write_text(json.dumps({"items": items, "meta": {"consolidated_ts": 123.0}}), encoding="utf-8")


async def test_migration_moves_only_important_and_keeps_a_backup(tmp_path):
    now = time.time()
    _old_file(tmp_path, now)
    g, tasks = _graph(tmp_path), _Tasks()
    done = await migrate(tmp_path, g, tasks, owner="Аня", clock=lambda: now)
    assert done == {"owner": 1, "self": 2, "plans": 1, "dropped": 2}
    assert [p[1] for p in tasks.plans] == ["Сдать отчёт"]
    assert not (tmp_path / FILE).exists() and (tmp_path / BACKUP).exists()
    assert json.loads((tmp_path / "memory_meta.json").read_text(encoding="utf-8")) == {"consolidated_ts": 123.0}
    assert await migrate(tmp_path, g, tasks, owner="Аня") == {}                                  # повторно ничего не делает


async def test_migration_ignores_unreadable_file(tmp_path):
    (tmp_path / FILE).write_text("не json", encoding="utf-8")
    assert await migrate(tmp_path, _graph(tmp_path), _Tasks()) == {}
    assert (tmp_path / FILE).exists()


# ── контекст ответа ───────────────────────────────────────────────────────────

def test_profile_is_in_context_even_off_topic(tmp_path):
    from app.context_builder import ContextBuilder
    g = _graph(tmp_path)
    g.remember("Пользователь любит зелёный чай по утрам", "owner", 5)
    ad = UserMemoryAdapter(lambda: None, lambda: g)

    class Host:
        _graph_block = _self_block = ""
        _memory_reader = ad
        _settings = SimpleNamespace(load=lambda: {"hearing": False})
        _people = SimpleNamespace(describe=lambda _: "")

        def _capabilities(self):
            return ""

        def _claude_note(self):
            return ""

    text = ContextBuilder(Host()).build(query="погода в Казани", needs_memory="no")
    assert "зелёный чай" in text


@pytest.mark.parametrize("name", ["memory_store", "knowledge", "lessons"])
def test_old_memory_layers_are_gone(name):
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)
