"""Том персонажа (CHARACTER_HOME): вся информация одного персонажа в одной папке (потом это том Docker).

Запуск из папки `agent`:

    python -m tools.character_home export <id> <целевая_папка> [--resources-root R] [--memory-root M]
    python -m tools.character_home check <папка>

Раскладка тома: `core/ voice/ pet/ ...` (содержимое `resources/<Имя>/`) и `memory/` (папка памяти персонажа). В память не
копируются журналы, кэши и файлы WAL; базы SQLite копируются через backup API. Исходные папки не меняются. Печатаются только
счётчики и пути, содержимое файлов не показывается.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "sdk"))
sys.path.insert(0, str(HERE.parents[1] / "agent"))

from gits_sdk import resources  # noqa: E402

MEMORY_SKIP_DIRS = {"logs", "__pycache__"}
MEMORY_SKIP_FILES = ("*.log", "*.tmp", "*-wal", "*-shm", "tool_embeddings.json", ".env", "module_token")
RESOURCE_SKIP_DIRS = {"__pycache__"}
RESOURCE_SKIP_FILES = (".env", "*.tmp")
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")


class HomeError(Exception):
    pass


def _skip(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def _copy_db(src: Path, dst: Path) -> None:
    """Согласованная копия SQLite (WAL сброшен в результат): backup API вместо копирования файла."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
    finally:
        s.close()


def _copy_tree(src: Path, dst: Path, skip_dirs, skip_files, stats: dict) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        if entry.is_dir():
            if entry.name in skip_dirs:
                continue
            _copy_tree(entry, dst / entry.name, skip_dirs, skip_files, stats)
        elif entry.is_file():
            if _skip(entry.name, skip_files):
                stats["skipped"] += 1
                continue
            if entry.suffix.lower() in DB_SUFFIXES:
                _copy_db(entry, dst / entry.name)
                stats["dbs"] += 1
            else:
                shutil.copy2(entry, dst / entry.name)
            stats["files"] += 1


def source_memory_dir(char_id: str, memory_root: Path) -> Path:
    """Папка памяти персонажа по правилам ядра (для персонажа по умолчанию корень памяти)."""
    return memory_root if char_id == resources.DEFAULT_ID else memory_root / "characters" / char_id


def export(char_id: str, target: Path, resources_root: Path | None = None, memory_root: Path | None = None) -> dict:
    target = Path(target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise HomeError(f"целевая папка не пуста или не папка: {target}")
    folder = resources.character_folders(resources_root or resources.resources_root()).get(char_id)
    if folder is None:
        raise HomeError(f"нет персонажа «{char_id}» в {resources_root or resources.resources_root()}")
    if memory_root is None:
        memory_root = Path(os.environ.get("MEMORY_DIR") or HERE.parents[1] / "memory")
    mem = source_memory_dir(char_id, Path(memory_root))
    target.mkdir(parents=True, exist_ok=True)
    res = {"files": 0, "dbs": 0, "skipped": 0}
    _copy_tree(folder, target, RESOURCE_SKIP_DIRS, RESOURCE_SKIP_FILES, res)
    mem_stats = {"files": 0, "dbs": 0, "skipped": 0}
    (target / "memory").mkdir(exist_ok=True)
    if mem.is_dir():
        skip_dirs = MEMORY_SKIP_DIRS | ({"characters"} if char_id == resources.DEFAULT_ID else set())   # чужие персонажи не в этом томе
        _copy_tree(mem, target / "memory", skip_dirs, MEMORY_SKIP_FILES, mem_stats)
    return {"id": char_id, "target": str(target), "resources": res, "memory": mem_stats, "memory_source": str(mem)}


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check(home: Path) -> dict:
    home = Path(home)
    cid = resources.card_id(home)
    if not cid:
        raise HomeError(f"{home}: нет {resources.CARD_FILE} с полем id")
    if not (home / "memory").is_dir():
        raise HomeError(f"{home}: нет папки memory/")
    return {"id": cid, "home": str(home), "resources_bytes": _size(home) - _size(home / "memory"),
            "memory_bytes": _size(home / "memory")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.character_home", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="собрать том персонажа")
    e.add_argument("id")
    e.add_argument("target")
    e.add_argument("--resources-root")
    e.add_argument("--memory-root", help="корень памяти ядра (по умолчанию MEMORY_DIR или ./memory репозитория)")
    c = sub.add_parser("check", help="проверить том")
    c.add_argument("home")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "export":
            r = export(a.id, Path(a.target), Path(a.resources_root) if a.resources_root else None,
                       Path(a.memory_root) if a.memory_root else None)
            print(f"том {r['id']}: {r['target']}")
            print(f"ресурсы: файлов {r['resources']['files']} (баз {r['resources']['dbs']}), пропущено {r['resources']['skipped']}")
            print(f"память из {r['memory_source']}: файлов {r['memory']['files']} (баз {r['memory']['dbs']}), "
                  f"пропущено {r['memory']['skipped']}")
        else:
            r = check(Path(a.home))
            print(f"том в порядке: id {r['id']}, ресурсы {r['resources_bytes']} байт, память {r['memory_bytes']} байт")
    except HomeError as ex:
        print(f"ошибка: {ex}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
