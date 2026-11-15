"""Разовый перенос старой памяти-графа (`graph.db`, таблица `facts`) в эпизоды персонажа.

Запускается при открытии памяти персонажа (`EpisodeService`), идемпотентно: отметка `migrated_graph` в `meta` базы эпизодов, а сам файл
после переноса переименовывается в `graph.db.migrated` (не удаляется). Что куда:
  действующие воспоминания персонажа (`canon=1`, есть `mem_id`)  -> эпизоды `kind=canon` (дальше их ведёт загрузка из memories.yaml);
  все остальные действующие факты (заметки о владельце, факты о людях и жизни)  -> заметки `kind=note`, яркость = важность / 5;
  состояние колонистов и колонии (отношения КОЛОНИСТ, НАВЫКИ, ЧЕРТЫ, СОСТОЯНИЕ, УПРАВЛЯЕТ, СОСТАВ)  не переносится: это состояние игры.
Текст заметки самодостаточный: если в факте нет имени субъекта, оно добавляется в начало («Аня: любит чай»). Эмбеддинг старого документа
переносится, когда текст не изменился, иначе вектор добавит сон. Закрытые (`invalid_at`) факты отбрасываются. Берутся факты группы
активного персонажа (`<id>` и `<id>:<область>`)."""
import array
import hashlib
import logging
import re
import sqlite3
import time
from pathlib import Path

from services.memory_service import episodes as dom
from services.memory_service.episode_store import EpisodeStore
from services.memory_service.search.index import HybridIndex

logger = logging.getLogger("episode_memory")

GRAPH_FILE = "graph.db"
MIGRATED_SUFFIX = ".migrated"
MARK = "migrated_graph"
WORLD_RELATIONS = frozenset({"КОЛОНИСТ", "НАВЫКИ", "ЧЕРТЫ", "СОСТОЯНИЕ", "УПРАВЛЯЕТ", "СОСТАВ"})


def self_sufficient(subject: str, fact: str) -> str:
    """Факт одной фразой с именем субъекта: если имени в тексте нет, оно идёт в начало."""
    fact = re.sub(r"\s+", " ", str(fact)).strip()
    subject = re.sub(r"\s+", " ", str(subject or "")).strip()
    if subject and subject.lower()[:max(4, len(subject) - 2)] not in fact.lower():          # основа: «пользователь» найдёт «пользователю»
        return f"{subject}: {fact}"
    return fact


def _vectors(db: sqlite3.Connection) -> dict[str, list[float]]:
    """uuid факта -> вектор его документа в старом индексе (если индекса и векторов нет, пусто)."""
    try:
        rows = db.execute("SELECT key, vec FROM docs WHERE vec IS NOT NULL").fetchall()
    except sqlite3.DatabaseError:
        return {}
    out = {}
    for key, blob in rows:
        a = array.array("f")
        a.frombytes(blob)
        out[key] = list(a)
    return out


def _rename(path: Path) -> None:
    target = path.with_name(path.name + MIGRATED_SUFFIX)
    if target.exists():
        target = path.with_name(f"{path.name}{MIGRATED_SUFFIX}.{int(time.time())}")
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(path) + suffix)
        if src.exists():
            src.replace(Path(str(target) + suffix))


def migrate_graph(folder: Path, store: EpisodeStore, index: HybridIndex, char_id: str, clock=time.time) -> dict | None:
    """Перенести `folder/graph.db`. None, если переносить нечего (уже перенесено или файла нет); иначе счётчики {canon, notes, skipped}."""
    path = Path(folder) / GRAPH_FILE
    if store.get_meta(MARK) or not path.is_file():
        return None
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    done = {"canon": 0, "notes": 0, "skipped": 0}
    try:
        have = {r[1] for r in db.execute("PRAGMA table_info(facts)")}
        if not have:
            raise sqlite3.DatabaseError("нет таблицы facts")
        cols = ["uuid", "group_id", "subject", "relation", "fact", "valid_at", "created_at", "canon", "mem_id", "mtype", "weight",
                "importance", "profile"]
        sel = ", ".join(c if c in have else f"NULL AS {c}" for c in cols)
        rows = db.execute(f"SELECT {sel} FROM facts WHERE invalid_at IS NULL ORDER BY created_at").fetchall()
        vecs = _vectors(db)
        for r in rows:
            g = r["group_id"] or ""
            if g != char_id and not g.startswith(char_id + ":"):
                done["skipped"] += 1
                continue
            if str(r["relation"] or "").upper() in WORLD_RELATIONS or not str(r["fact"] or "").strip():
                done["skipped"] += 1
                continue
            ts = r["valid_at"] or r["created_at"] or clock()
            if r["canon"] and r["mem_id"]:
                text = re.sub(r"\s+", " ", r["fact"]).strip()
                weight = float(r["weight"] or 0.5)
                res = store.upsert_canon(r["mem_id"], text, mtype=r["mtype"] or "self", weight=weight, character=char_id)
                ep_id, kind, sal = f"canon:{r['mem_id']}", dom.CANON, weight
                done["canon"] += res == "added"
            else:
                text = self_sufficient(r["subject"], r["fact"])
                sal = max(1, min(5, int(r["importance"] or 3))) / 5
                ep_id, kind = "n-" + hashlib.sha1(text.lower().encode("utf-8")).hexdigest()[:12], dom.NOTE
                if not store.add_note(ep_id, text, salience=sal, ts=ts, character=char_id):
                    done["skipped"] += 1
                    continue
                done["notes"] += 1
            vec = vecs.get(r["uuid"]) if text == re.sub(r"\s+", " ", str(r["fact"])).strip() else None
            index.upsert("episodes", ep_id, dom.index_text("", text), vec=vec, ts=ts,
                         meta={"episode": ep_id, "kind": kind, "salience": sal})
    except sqlite3.DatabaseError as e:
        logger.warning(f"Перенос graph.db не удался, файл оставлен как есть: {e}")
        return None
    finally:
        db.close()
    store.set_meta(MARK, {**done, "at": clock()})
    try:
        _rename(path)
    except OSError as e:
        logger.warning(f"graph.db перенесён, но не переименован ({e}): отметка стоит, повтора не будет")
    return done
