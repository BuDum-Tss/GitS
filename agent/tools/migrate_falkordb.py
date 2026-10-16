"""Разовый перенос памяти-графа из FalkorDB (Graphiti) в SQLite `graph.db`.

Запуск из папки `agent`, пока контейнер графа (`docker ps`) ещё работает (нужен пакет `falkordb`: `pip install falkordb`, он больше
нигде не нужен):

    python -m tools.migrate_falkordb --dry-run            # посмотреть, что есть в графе, ничего не писать
    python -m tools.migrate_falkordb                       # перенести (LM Studio с EmbeddingGemma нужна для векторов)
    python -m tools.migrate_falkordb --no-embed            # перенести без векторов (поиск только по словам)

Переносятся все связи `RELATES_TO` всех графов (одна база = один персонаж) с датами, каноном, воспоминаниями персонажа и
группами. Повторный запуск безопасен: связи с уже известным uuid пропускаются. После переноса контейнер и том можно удалять.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

QUERY = ("MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity) RETURN a.name, b.name, r.uuid, r.name, r.fact, r.group_id, a.group_id, "
         "r.valid_at, r.invalid_at, r.created_at, r.canon, r.source, r.mem_id, r.mtype, r.about, r.weight")


def to_ts(value) -> float | None:
    """Дата из графа (строка ISO, datetime или число секунд) в секунды Unix."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def row_of(r: list, graph: str) -> dict | None:
    """Строка результата запроса в запись для `FactStore.import_row`; None, если связь неполная."""
    subject, obj, uuid, relation, fact, group, agroup, valid, invalid, created, canon, source, mem_id, mtype, about, weight = r
    if not (subject and obj and uuid and relation and fact):
        return None
    return {"uuid": str(uuid), "group_id": group or agroup or graph, "subject": str(subject), "relation": str(relation),
            "object": str(obj), "fact": str(fact), "valid_at": to_ts(valid), "invalid_at": to_ts(invalid),
            "created_at": to_ts(created) or to_ts(valid), "canon": bool(canon), "source": source or "extract",
            "mem_id": mem_id, "mtype": mtype, "about": about, "weight": float(weight or 0)}


def read_falkordb(host: str, port: int) -> Iterator[dict]:
    from falkordb import FalkorDB
    db = FalkorDB(host=host, port=port)
    for name in db.list_graphs():
        for r in db.select_graph(name).query(QUERY).result_set:
            row = row_of(r, name)
            if row:
                yield row


async def migrate(rows: Iterable[dict], backend, *, embed: bool = True, batch: int = 32) -> dict[str, int]:
    """Записать связи в хранилище; векторы считаются пачками. Возвращает счётчики: imported, skipped, embedded."""
    counts = {"imported": 0, "skipped": 0, "embedded": 0}
    fresh: list[dict] = []
    for row in rows:
        if backend.store.import_row(row):
            counts["imported"] += 1
            fresh.append(row)
        else:
            counts["skipped"] += 1
    step = batch if embed else max(len(fresh), 1)
    for i in range(0, len(fresh), step):
        chunk = fresh[i:i + step]
        vecs = await backend.embedder.embed([r["fact"] for r in chunk], False) if embed else None
        for n, r in enumerate(chunk):
            backend.search_service.index.upsert(r["group_id"], r["uuid"], r["fact"], vec=vecs[n] if vecs else None,
                                                ts=r["valid_at"] or r["created_at"])
        counts["embedded"] += len(chunk) if vecs else 0
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.migrate_falkordb", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.getenv("GITS_GRAPH_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("GITS_GRAPH_PORT", "6380")))
    ap.add_argument("--memory-dir", default=os.getenv("MEMORY_DIR", "./memory"), help="папка памяти ядра (туда ляжет graph.db)")
    ap.add_argument("--dry-run", action="store_true", help="только прочитать граф и показать счётчики по группам")
    ap.add_argument("--no-embed", action="store_true", help="не считать векторы (поиск только по словам)")
    args = ap.parse_args(argv)
    try:
        rows = list(read_falkordb(args.host, args.port))
    except ImportError:
        print("нужен пакет falkordb: pip install falkordb", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"граф не отвечает ({args.host}:{args.port}): {e}", file=sys.stderr)
        return 2
    by_group: dict[str, int] = {}
    for r in rows:
        by_group[r["group_id"]] = by_group.get(r["group_id"], 0) + 1
    print(f"в графе {len(rows)} связей: " + ", ".join(f"{g} {n}" for g, n in sorted(by_group.items())))
    if args.dry_run:
        return 0
    from graph_memory import SqliteBackend
    backend = SqliteBackend(Path(args.memory_dir) / "graph.db")
    counts = asyncio.run(migrate(rows, backend, embed=not args.no_embed))
    print(f"перенесено {counts['imported']}, уже были {counts['skipped']}, векторов {counts['embedded']} -> "
          f"{Path(args.memory_dir) / 'graph.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
