"""Канон персонажа в эпизодах (ТЗ 05, CHR-16): воспоминания о себе из memories.yaml становятся эпизодами `kind=canon`.

`python -m services.memory_service.character_seed seed <id> [--dry-run]` (из папки agent) приводит канон в базе эпизодов персонажа к его
memories.yaml: новые записи добавляются, изменённые заменяются (обновление по `mem_id`, без дублей), удалённые из yaml удаляются.
То же ядро делает само при открытии памяти персонажа (`seed_canon`). Эпизод канона: текст записи, `mem_id`, тип и вес; он защищён
от инструментов забывания и записи (CHR-17): меняет его только эта загрузка. Модели и LM Studio не нужны: индекс по словам готов сразу,
вектор добавит сон, когда эмбеддер доступен.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from services.character import character
from services.character import character_memory
from services.memory_service import episodes as dom
from services.memory_service.episode_store import FILE, EpisodeStore, canon_id
from services.memory_service.search.index import HybridIndex

logger = logging.getLogger("episode_memory")


def seed_canon(char: character.Character, store: EpisodeStore, index: HybridIndex) -> dict[str, int]:
    """Привести канон в эпизодах к memories.yaml персонажа. Счётчики: added, updated, same, removed."""
    items = character_memory.load_memories(char.path(character.MEMORIES_FILE))
    counts = {"added": 0, "updated": 0, "same": 0, "removed": 0}
    for m in items:
        res = store.upsert_canon(m["id"], m["text"], mtype=m["type"], weight=m["weight"], character=char.id)
        counts[res] += 1
        if res != "same":                                    # новый или изменённый текст: индекс по словам сразу, вектор позже
            index.upsert("episodes", canon_id(m["id"]), dom.index_text("", m["text"]), ts=store.get(canon_id(m["id"]))["t_start"],
                         meta={"episode": canon_id(m["id"]), "kind": dom.CANON, "salience": m["weight"]})
    keep = {m["id"] for m in items}
    for mem_id in store.canon_ids() - keep:
        store.remove_canon(mem_id)
        index.delete("episodes", canon_id(mem_id))
        counts["removed"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.memory_service.character_seed", description="Персонажи GitS (ТЗ 05)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="загрузить воспоминания персонажа в эпизоды (по memories.yaml)")
    s.add_argument("id", help="id персонажа: папка resources/<Имя> с этим id")
    s.add_argument("--dry-run", action="store_true", help="только проверить файл, память не трогать")
    s.add_argument("--memory-dir", default=os.getenv("MEMORY_DIR", "./memory"), help="папка памяти ядра (там episodes.db)")
    args = ap.parse_args(argv)
    try:
        char = character.load(args.id)
        items = character_memory.load_memories(char.path(character.MEMORIES_FILE))
    except character.CharacterError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"{char.id}: записей в memories.yaml: {len(items)}, файл в порядке")
        return 0
    path = Path(args.memory_dir) / FILE
    store, index = EpisodeStore(path), HybridIndex(path)
    counts = seed_canon(char, store, index)
    print(f"{char.id}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
