"""Граф по персонажам (ТЗ 05, CHR-9, CHR-16..CHR-19): группы, защита «я» и канона, отбор о себе, загрузка memories.yaml.

Бэкенд подменён памятью (FakeGraph): FalkorDB и LM Studio в тестах не нужны. Настоящие запросы Cypher проверяются тем,
что строки запросов содержат защиту канона (test_canon_guard_is_in_every_cleanup_query)."""
import shutil
from datetime import datetime, timezone
from pathlib import Path

import character
import character_seed
import events_log
from graph_memory import Fact, GraphMemory

REAL_CHARACTERS = Path(__file__).resolve().parents[2] / "resources"


class FakeGraph:
    """Память графа в списке словарей: по одной записи на связь, как в FalkorDB."""

    def __init__(self):
        self.edges: list[dict] = []
        self.score_map: dict[str, float] = {}
        self.search_calls: list[tuple] = []

    async def healthy(self):
        return True, "работает"

    async def add_fact(self, f: Fact, when, group="glados") -> str:
        uuid = f"u{len(self.edges) + 1}"
        self.edges.append({"uuid": uuid, "group": group, "subject": f.subject, "relation": f.relation, "object": f.object,
                           "fact": f.fact, "canon": False, "source": "extract", "mem_id": None, "mtype": None,
                           "about": None, "weight": 0.0})
        return uuid

    async def has_canon(self, group, subject, relation) -> bool:
        return any(e["group"] == group and e["subject"] == subject and e["relation"] == relation and e["canon"]
                   for e in self.edges)

    async def self_memories(self, group, subject) -> list[dict]:
        return [{"mem_id": e["mem_id"], "uuid": e["uuid"], "fact": e["fact"], "mtype": e["mtype"], "weight": e["weight"],
                 "source": e["source"]}
                for e in self.edges if e["group"] == group and e["subject"] == subject and e["mtype"]]

    async def scores(self, query, facts):
        return [self.score_map.get(f, 0.0) for f in facts]

    async def search(self, query, limit, group="glados"):
        self.search_calls.append((query, group))
        return []

    async def seed_rows(self, group) -> dict:
        return {e["mem_id"]: (e["uuid"], e["fact"]) for e in self.edges if e["group"] == group and e["source"] == "seed"}

    async def mark_memory(self, group, uuid, mem_id, mtype, about, weight, source):
        for e in self.edges:
            if e["uuid"] == uuid:
                e.update(mem_id=mem_id, canon=source == "seed", source=source, mtype=mtype, about=about, weight=weight)

    async def delete_memory(self, group, mem_id):
        self.edges = [e for e in self.edges if not (e["group"] == group and e["mem_id"] == mem_id)]


class FakeExtractor:
    def __init__(self, facts=()):
        self.facts = list(facts)

    async def extract(self, speaker, text):
        return list(self.facts)


def _gm(tmp_path, backend, facts=()):
    gm = GraphMemory(str(tmp_path), backend=backend, extractor=FakeExtractor(facts), busy=lambda: False)
    return gm


async def test_new_facts_go_to_the_active_character_group(tmp_path):
    g = FakeGraph()
    gm = _gm(tmp_path, g, [Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня живёт в Казани")])
    gm.enqueue("Аня", "я теперь живу в Казани, переехала на прошлой неделе")
    assert await gm.step()
    assert [(e["group"], e["subject"]) for e in g.edges] == [("glados", "Аня")]


async def test_switching_the_character_moves_new_facts_not_the_queue(tmp_path):
    """CHR-9: очередь общая, но запись помечена персонажем на момент реплики."""
    g = FakeGraph()
    gm = _gm(tmp_path, g, [Fact("Аня", "ЖИВЁТ_В", "Казань", "Аня живёт в Казани")])
    gm.enqueue("Аня", "первая реплика достаточно длинная для разбора")
    gm.set_character("tester")
    gm.enqueue("Аня", "вторая реплика тоже достаточно длинная")
    assert [q["character"] for q in gm._queue] == ["glados", "tester"]
    await gm.step()
    await gm.step()
    assert sorted(e["group"] for e in g.edges) == ["glados", "tester"]


async def test_facts_about_the_character_from_a_user_message_are_not_written(tmp_path):
    """CHR-18: «я был в отпуске в Сочи» не создаёт воспоминание персонажа (субъект ГЛаДОС)."""
    g = FakeGraph()
    about_me = Fact("ГЛаДОС", "БЫЛ_В", "Сочи", "ГЛаДОС была в Сочи в отпуске")
    gm = _gm(tmp_path, g, [about_me])
    gm.enqueue("Аня", "я был в отпуске в Сочи, очень понравилось море")
    await gm.step()
    assert g.edges == []


async def test_a_canon_conflict_is_logged_and_the_canon_is_kept(tmp_path, monkeypatch):
    """CHR-17: противоречие канону пишется событием memory_conflict и не применяется."""
    logged = []
    monkeypatch.setattr(events_log, "log", lambda kind, text="", **data: logged.append((kind, text)))
    g = FakeGraph()
    g.edges.append({"uuid": "c1", "group": "glados", "subject": "ГЛаДОС", "relation": "ПОМНИТ", "object": "воспоминание m-1",
                    "fact": "канон", "canon": True, "source": "seed", "mem_id": "m-1", "mtype": "biography", "about": "self",
                    "weight": 0.8})
    gm = _gm(tmp_path, g, [Fact("ГЛаДОС", "ПОМНИТ", "что-то", "ГЛаДОС помнит другое")])
    gm.enqueue("Аня", "ты помнишь что-то другое о себе, расскажи подробнее")
    await gm.step()
    assert len(g.edges) == 1, "канон не изменён и новое не записано"
    assert ("memory_conflict", "канон сохранён, факт не применён: ГЛаДОС помнит другое") in logged


async def test_self_memories_follow_threshold_and_nudge_every_fifth_quiet_reply(tmp_path):
    """CHR-19: при пустом отборе раз в пять реплик напоминание о привычке, потом счётчик сбрасывается."""
    g = FakeGraph()
    g.edges.append({"uuid": "h1", "group": "glados", "subject": "ГЛаДОС", "relation": "ПОМНИТ", "object": "воспоминание m-6",
                    "fact": "люблю паузы подытоживать", "canon": True, "source": "seed", "mem_id": "m-6",
                    "mtype": "habit", "about": "self", "weight": 0.6})
    gm = _gm(tmp_path, g)
    async def ready():
        return True
    gm._ready = ready
    got = []
    for _ in range(5):
        got.append(await gm.self_memories("совсем другая тема про погоду"))
    assert got[:4] == [[], [], [], []]
    assert [m["mem_id"] for m in got[4]] == ["m-6"] and got[4][0]["nudge"] is True
    assert await gm.self_memories("ещё погода") == [], "после напоминания счётчик начинается заново"


async def test_self_memories_pick_relevant_ones_for_the_reply(tmp_path):
    g = FakeGraph()
    g.edges.append({"uuid": "o1", "group": "glados", "subject": "ГЛаДОС", "relation": "ПОМНИТ", "object": "воспоминание m-3",
                    "fact": "о людях и ошибках", "canon": True, "source": "seed", "mem_id": "m-3", "mtype": "opinion",
                    "about": "self", "weight": 0.75})
    g.score_map["о людях и ошибках"] = 0.8
    gm = _gm(tmp_path, g)

    async def ready():
        return True
    gm._ready = ready
    picked = await gm.self_memories("люди вечно ошибаются")
    assert [m["mem_id"] for m in picked] == ["m-3"]


async def test_self_memories_of_another_character_are_not_visible(tmp_path):
    """CHR-9: граф персонажа не видит фактов графа другого персонажа."""
    g = FakeGraph()
    g.edges.append({"uuid": "x", "group": "glados", "subject": "ГЛаДОС", "relation": "ПОМНИТ", "object": "o",
                    "fact": "чужое", "canon": True, "source": "seed", "mem_id": "m-1", "mtype": "opinion", "about": "self",
                    "weight": 0.9})
    g.score_map["чужое"] = 0.9
    gm = _gm(tmp_path, g)
    gm.set_character("tester")

    async def ready():
        return True
    gm._ready = ready
    assert await gm.self_memories("чужое") == []


async def test_search_uses_the_active_group(tmp_path):
    g = FakeGraph()
    gm = _gm(tmp_path, g)
    gm._healthy = True
    gm._health_at = float("inf")
    gm.set_character("tester")
    await gm.search("вопрос")
    assert g.search_calls == [("вопрос", "tester")]


def _copy_characters(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "characters"
    shutil.copytree(REAL_CHARACTERS / "GlaDOS", root / "GlaDOS")
    monkeypatch.setattr(character, "CHARACTERS_DIR", root)
    return root


async def test_seed_adds_then_updates_by_id_without_duplicates(tmp_path, monkeypatch):
    """CHR-16: загрузка по memories.yaml: новые добавлены, повторная не дублирует, изменённая заменена, удалённая убрана."""
    root = _copy_characters(tmp_path, monkeypatch)
    ch = character.load("glados")
    g = FakeGraph()
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    first = await character_seed.seed(ch, g, when)
    assert first == {"added": 10, "updated": 0, "same": 0, "removed": 0}
    assert len(g.edges) == 10
    assert all(e["subject"] == ch.name and e["group"] == "glados" and e["canon"] and e["source"] == "seed" for e in g.edges)

    again = await character_seed.seed(ch, g, when)
    assert again == {"added": 0, "updated": 0, "same": 10, "removed": 0} and len(g.edges) == 10

    mem = root / "GlaDOS" / "core" / "memories.yaml"
    text = mem.read_text(encoding="utf-8")
    text = text.replace("хотя понимаю, что это шутка.", "и не объясняю почему.")
    mem.write_text(text, encoding="utf-8")
    edited = await character_seed.seed(character.load("glados"), g, when)
    assert edited["same"] == 9 and edited["updated"] == 1 and len(g.edges) == 10

    # запись удалена из файла: в графе её быть не должно
    kept = text.splitlines()
    cut = next(i for i, line in enumerate(kept) if line.startswith("- id: m-0010"))
    mem.write_text("\n".join(kept[:cut]) + "\n", encoding="utf-8")
    removed = await character_seed.seed(character.load("glados"), g, when)
    assert removed["removed"] == 1 and len(g.edges) == 9


def test_seed_dry_run_reads_the_file_without_the_graph(capsys):
    assert character_seed.main(["seed", "glados", "--dry-run"]) == 0
    assert "записей в memories.yaml: 10" in capsys.readouterr().out


def test_seed_reports_a_broken_card_as_an_error(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(character, "CHARACTERS_DIR", tmp_path)
    assert character_seed.main(["seed", "nobody", "--dry-run"]) == 2
    assert "ошибка" in capsys.readouterr().err


def test_canon_guard_is_in_every_cleanup_query():
    """CHR-17: колонисты, закрытие фактов и очистка не трогают канонические связи (проверка строк Cypher)."""
    src = (Path(__file__).resolve().parents[1] / "graph_memory.py").read_text(encoding="utf-8")
    assert src.count("r.canon IS NULL OR r.canon = false") == 3


def test_events_kinds_include_memory_conflict():
    assert "memory_conflict" in events_log.KINDS
