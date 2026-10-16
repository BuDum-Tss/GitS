"""Хранилище фактов памяти-графа в SQLite (вместо Graphiti и FalkorDB): таблица `facts`, один файл `graph.db`.

Факт это связь «субъект - отношение - объект» с текстом, интервалом действия (`valid_at`, `invalid_at`) и пометками
воспоминаний персонажа (канон, тип, вес). Группа (`group_id`) та же, что была в графе: `<id персонажа>` и `<id>:<область>`.
Поиск по смыслу и словам делает общий `search.index.HybridIndex` в том же файле (коллекция = группа, ключ = uuid факта);
здесь только структура и правила устаревания. Чисто синхронный код: асинхронная обёртка в `graph_memory.SqliteBackend`.

Устаревание без модели: у однозначных отношений (`ЖИВЁТ_В`, `РАБОТАЕТ`...) новый факт закрывает прежний датой; точный повтор
той же связи не дублируется. Канонические факты персонажа (`canon = 1`) не закрываются и не удаляются ничем, кроме загрузки
из memories.yaml."""
import sqlite3
import threading
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path

# отношения с одним значением на субъекта: новый факт закрывает старый (остальные, вроде «подруга», накапливаются)
SINGLE_VALUED = frozenset({
    "ЖИВЕТ_В", "ЖИВЕТ", "РАБОТАЕТ", "РАБОТАЕТ_В", "РАБОТАЕТ_КЕМ", "УЧИТСЯ", "УЧИТСЯ_В", "ЗОВУТ", "ИМЯ", "ВОЗРАСТ",
    "ДОЛЖНОСТЬ", "ПРОФЕССИЯ", "ГОРОД", "МЕСТО_ЖИТЕЛЬСТВА", "СЕМЕЙНОЕ_ПОЛОЖЕНИЕ", "ДАТА_РОЖДЕНИЯ", "ДЕНЬ_РОЖДЕНИЯ"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    uuid TEXT PRIMARY KEY, group_id TEXT NOT NULL,
    subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL, fact TEXT NOT NULL,
    subj_key TEXT NOT NULL, obj_key TEXT NOT NULL,
    valid_at REAL, invalid_at REAL, created_at REAL NOT NULL,
    canon INTEGER NOT NULL DEFAULT 0, source TEXT, mem_id TEXT, mtype TEXT, about TEXT, weight REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS facts_subject ON facts (group_id, subj_key, relation);
CREATE INDEX IF NOT EXISTS facts_mem ON facts (group_id, mem_id);
CREATE INDEX IF NOT EXISTS facts_object ON facts (obj_key);
"""
_COLS = ("uuid", "group_id", "subject", "relation", "object", "fact", "valid_at", "invalid_at", "canon", "source", "mem_id",
         "mtype", "about", "weight")


def rel_key(relation: str) -> str:
    return relation.upper().replace("Ё", "Е")


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


class FactStore:
    def __init__(self, path: Path | str = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")      # поиск (HybridIndex) пишет в тот же файл другим соединением
            except sqlite3.DatabaseError:
                pass
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ── запись ─────────────────────────────────────────────────────────────────

    def add(self, subject: str, relation: str, obj: str, fact: str, when: float, group: str, *, now: float,
            canon: bool = False, source: str = "extract") -> tuple[str, bool]:
        """Записать факт. (uuid, новый ли): повтор действующей связи не дублируется, однозначное отношение закрывает старое."""
        sk, ok = subject.lower(), obj.lower()
        with self._lock:
            same = self._db.execute("SELECT uuid FROM facts WHERE group_id=? AND subj_key=? AND relation=? AND obj_key=? "
                                    "AND invalid_at IS NULL", (group, sk, relation, ok)).fetchone()
            if same:
                return same["uuid"], False
            if rel_key(relation) in SINGLE_VALUED:
                self._db.execute("UPDATE facts SET invalid_at=? WHERE group_id=? AND subj_key=? AND relation=? "
                                 "AND invalid_at IS NULL AND canon=0", (when, group, sk, relation))
            new = uuidlib.uuid4().hex
            self._db.execute("INSERT INTO facts (uuid, group_id, subject, relation, object, fact, subj_key, obj_key, valid_at, "
                             "created_at, canon, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (new, group, subject, relation, obj, fact, sk, ok, when, now, int(canon), source))
            self._db.commit()
        return new, True

    def replace_relation(self, subject: str, relation: str, group: str) -> list[str]:
        """Удалить некононические факты субъекта с этим отношением (данные игры перезаписываются); uuid удалённых."""
        with self._lock:
            ids = [r["uuid"] for r in self._db.execute(
                "SELECT uuid FROM facts WHERE group_id=? AND subj_key=? AND relation=? AND canon=0",
                (group, subject.lower(), relation)).fetchall()]
            self._db.execute("DELETE FROM facts WHERE group_id=? AND subj_key=? AND relation=? AND canon=0",
                             (group, subject.lower(), relation))
            self._db.commit()
        return ids

    def retire(self, subject: str, relations: tuple, group: str, now: float) -> int:
        """Закрыть датой действующие некононические факты субъекта с указанными отношениями."""
        marks = ",".join("?" * len(relations))
        with self._lock:
            cur = self._db.execute(f"UPDATE facts SET invalid_at=? WHERE group_id=? AND subj_key=? AND relation IN ({marks}) "
                                   "AND invalid_at IS NULL AND canon=0", (now, group, subject.lower(), *relations))
            self._db.commit()
            return cur.rowcount

    def purge(self, relations: tuple, group: str, before: float) -> list[str]:
        """Удалить закрытые раньше `before` некононические факты с указанными отношениями; uuid удалённых."""
        marks = ",".join("?" * len(relations))
        with self._lock:
            ids = [r["uuid"] for r in self._db.execute(
                f"SELECT uuid FROM facts WHERE group_id=? AND relation IN ({marks}) AND invalid_at IS NOT NULL "
                "AND invalid_at<? AND canon=0", (group, *relations, before)).fetchall()]
            for i in ids:
                self._db.execute("DELETE FROM facts WHERE uuid=?", (i,))
            self._db.commit()
        return ids

    def delete(self, uuid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM facts WHERE uuid=?", (uuid,))
            self._db.commit()
            return cur.rowcount > 0

    # ── чтение ─────────────────────────────────────────────────────────────────

    def get(self, uuids: list[str]) -> dict[str, dict]:
        if not uuids:
            return {}
        with self._lock:
            rows = self._db.execute(f"SELECT {','.join(_COLS)} FROM facts WHERE uuid IN ({','.join('?' * len(uuids))})",
                                    uuids).fetchall()
        return {r["uuid"]: dict(r) for r in rows}

    def has_canon(self, group: str, subject: str, relation: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM facts WHERE group_id=? AND subj_key=? AND relation=? AND canon=1 "
                                    "AND invalid_at IS NULL", (group, subject.lower(), relation)).fetchone() is not None

    def groups_with_entities(self, group: str, names: list[str]) -> list[str]:
        """Области того же персонажа (`group` и `group:*`), где есть сущность с одним из имён (без регистра)."""
        keys = [n.lower() for n in names]
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        with self._lock:
            rows = self._db.execute(
                f"SELECT DISTINCT group_id FROM facts WHERE (group_id=? OR substr(group_id, 1, ?)=?) "
                f"AND (subj_key IN ({marks}) OR obj_key IN ({marks}))",
                (group, len(group) + 1, group + ":", *keys, *keys)).fetchall()
        return [r["group_id"] for r in rows]

    def seed_rows(self, group: str) -> dict[str, tuple[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT mem_id, uuid, fact FROM facts WHERE group_id=? AND source='seed' "
                                    "AND mem_id IS NOT NULL", (group,)).fetchall()
        return {r["mem_id"]: (r["uuid"], r["fact"]) for r in rows}

    def mark(self, uuid: str, mem_id: str, mtype: str, about: str, weight: float, source: str) -> None:
        with self._lock:
            self._db.execute("UPDATE facts SET mem_id=?, canon=?, source=?, mtype=?, about=?, weight=? WHERE uuid=?",
                             (mem_id, int(source == "seed"), source, mtype, about, float(weight), uuid))
            self._db.commit()

    def delete_memory(self, group: str, mem_id: str) -> list[str]:
        with self._lock:
            ids = [r["uuid"] for r in self._db.execute("SELECT uuid FROM facts WHERE group_id=? AND mem_id=?",
                                                       (group, mem_id)).fetchall()]
            self._db.execute("DELETE FROM facts WHERE group_id=? AND mem_id=?", (group, mem_id))
            self._db.commit()
        return ids

    def self_memories(self, group: str, subject: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT mem_id, uuid, fact, mtype, weight, source FROM facts WHERE group_id=? AND subj_key=? "
                                    "AND mtype IS NOT NULL AND invalid_at IS NULL", (group, subject.lower())).fetchall()
        return [{"mem_id": r["mem_id"], "uuid": r["uuid"], "fact": r["fact"], "mtype": r["mtype"],
                 "weight": float(r["weight"] or 0), "source": r["source"]} for r in rows]

    def snapshot(self, group: str, limit: int) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT subject, relation, object, fact, uuid, valid_at, invalid_at FROM facts "
                                    "WHERE group_id=? ORDER BY created_at DESC LIMIT ?", (group, limit)).fetchall()
        names = list(dict.fromkeys(n for r in rows for n in (r["subject"], r["object"])))
        edges = [{"from": r["subject"], "relation": r["relation"], "to": r["object"], "fact": r["fact"], "uuid": r["uuid"],
                  "valid_at": (iso(r["valid_at"]) or "")[:10] or None, "invalid_at": (iso(r["invalid_at"]) or "")[:10] or None}
                 for r in rows]
        return {"entities": names, "edges": edges}

    def import_row(self, row: dict) -> bool:
        """Перенос готовой связи (из старого графа) как есть; False, если такой uuid уже есть."""
        subject, obj = row["subject"], row["object"]
        with self._lock:
            if self._db.execute("SELECT 1 FROM facts WHERE uuid=?", (row["uuid"],)).fetchone():
                return False
            self._db.execute(
                "INSERT INTO facts (uuid, group_id, subject, relation, object, fact, subj_key, obj_key, valid_at, invalid_at, "
                "created_at, canon, source, mem_id, mtype, about, weight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["uuid"], row["group_id"], subject, row["relation"], obj, row["fact"], subject.lower(), obj.lower(),
                 row.get("valid_at"), row.get("invalid_at"), row.get("created_at") or row.get("valid_at") or 0.0,
                 int(bool(row.get("canon"))), row.get("source"), row.get("mem_id"), row.get("mtype"), row.get("about"),
                 float(row.get("weight") or 0)))
            self._db.commit()
        return True

    def count(self, group: str | None = None) -> int:
        with self._lock:
            if group is None:
                return self._db.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM facts WHERE group_id=?", (group,)).fetchone()[0]
