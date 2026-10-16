"""Загрузка воспоминаний персонажа в граф (ТЗ 05, CHR-16): `python -m character seed <id> [--dry-run]`.

Граф персонажа (группа = id, ТЗ 05 CHR-9) приводится к memories.yaml: новые записи добавляются, изменённые заменяются
(обновление по id, без дублей), удалённые из YAML удаляются. Факт: субъект — имя персонажа, отношение ПОМНИТ, объект —
сама запись; связь помечена canon: true, source: seed, с типом и весом. Нужны LM Studio (эмбеддинги) и файл `graph.db` в папке памяти (`--memory-dir`, по умолчанию `MEMORY_DIR` или `./memory`).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import character
import character_memory
from graph_memory import Fact

RELATION = "ПОМНИТ"


async def seed(char: character.Character, backend, when: datetime | None = None) -> dict[str, int]:
    """Привести граф персонажа к его memories.yaml. Возвращает счётчики: added, updated, same, removed."""
    group = character.group_id(char.id)
    items = character_memory.load_memories(char.path(character.MEMORIES_FILE))
    when = when or datetime.now(timezone.utc)
    rows = await backend.seed_rows(group)
    counts = {"added": 0, "updated": 0, "same": 0, "removed": 0}
    keep: set[str] = set()
    for m in items:
        keep.add(m["id"])
        about = " ".join(m["about"])
        old = rows.get(m["id"])
        if old and old[1] == m["text"]:
            counts["same"] += 1
            await backend.mark_memory(group, old[0], m["id"], m["type"], about, m["weight"], "seed")
            continue
        if old:
            await backend.delete_memory(group, m["id"])
            counts["updated"] += 1
        else:
            counts["added"] += 1
        fact = Fact(subject=char.name, relation=RELATION, object=f"воспоминание {m['id']}", fact=m["text"])
        uuid = await backend.add_fact(fact, when, group)
        await backend.mark_memory(group, uuid, m["id"], m["type"], about, m["weight"], "seed")
    for mem_id in rows:
        if mem_id not in keep:
            await backend.delete_memory(group, mem_id)
            counts["removed"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m character", description="Персонажи GitS (ТЗ 05)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="загрузить воспоминания персонажа в граф (по memories.yaml)")
    s.add_argument("id", help="id персонажа: папка resources/<Имя> с этим id")
    s.add_argument("--dry-run", action="store_true", help="только проверить файл, граф не трогать")
    s.add_argument("--memory-dir", default=os.getenv("MEMORY_DIR", "./memory"), help="папка памяти ядра (там graph.db)")
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
    from graph_memory import SqliteBackend
    counts = asyncio.run(seed(char, SqliteBackend(Path(args.memory_dir) / "graph.db")))
    print(f"{char.id}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
