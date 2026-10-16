"""Загрузка справочников в базу знаний ядра (ТЗ 04, KB-18, KB-19). Ручной запуск, без сети.

    python -m tools.knowledge_ingest dir rimworld.wiki C:/wiki_dump          # .md/.txt/.wiki/.html из папки (вики-дамп)
    python -m tools.knowledge_ingest defs rimworld.defs C:/RimWorld/Data/Core/Defs   # XML Defs: defName, label, description
    python -m tools.knowledge_ingest montage                                  # регламенты modules/montage -> montage.style

Ключи: `--hash` (детерминированный эмбеддер без LM Studio, для проверки), `--db путь` (по умолчанию `memory/knowledge.db`),
`--keep` (не удалять то, чего в загрузке не было). Повторный запуск обновляет куски по `uri#номер` и убирает пропавшие."""
import argparse
import asyncio
import html
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "sdk")]

TEXT_EXT = {".md", ".txt", ".wiki", ".html", ".htm"}
BATCH = 50


def wiki_to_text(raw: str) -> str:
    """Вики-разметка и HTML -> простой текст с заголовками `#` (шаблоны, ссылки, теги убираются)."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    t = re.sub(r"\{\{[^{}]*\}\}", " ", t)
    t = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"^(={2,})\s*(.+?)\s*\1\s*$", lambda m: "#" * len(m.group(1)) + " " + m.group(2), t, flags=re.M)
    t = re.sub(r"'{2,}", "", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"[ \t]+", " ", html.unescape(t)).strip()


def dir_documents(folder: Path) -> list[dict]:
    docs = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in TEXT_EXT:
            text = wiki_to_text(p.read_text(encoding="utf-8", errors="replace"))
            if text:
                docs.append({"uri": p.relative_to(folder).as_posix(), "title": p.stem.replace("_", " "), "text": text})
    return docs


def defs_documents(folder: Path) -> list[dict]:
    """XML Defs игры: по документу на определение (имя, ярлык, описание, родитель)."""
    docs = []
    for p in sorted(folder.rglob("*.xml")):
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError:
            continue
        for d in root:
            name = d.findtext("defName")
            label, desc = d.findtext("label"), d.findtext("description")
            if not name or not (label or desc):
                continue
            parts = [f"{label or name} ({d.tag}, {name})"]
            if d.get("ParentName"):
                parts.append(f"Родитель: {d.get('ParentName')}")
            if desc:
                parts.append(" ".join(desc.split()))
            docs.append({"uri": f"def:{d.tag}:{name}", "title": label or name, "text": "\n".join(parts),
                         "meta": {"kind": d.tag}})
    return docs


def montage_documents() -> list[dict]:
    base = HERE.parent.parent
    docs = []
    for p, uri, title in ((base / "modules/montage/docs/EDITING_CRAFT.md", "montage:craft", "Ремесло монтажа"),
                          *((f, "montage:style", "Регламент монтажа") for f in sorted(base.glob("resources/*/montage/*_STYLE.md")))):
        if p.exists():
            docs.append({"uri": uri, "title": title, "text": p.read_text(encoding="utf-8")})
    return docs


async def ingest(kb, collection: str, docs: list[dict], keep: bool) -> dict:
    total = {"added": 0, "updated": 0, "skipped": 0}
    for i in range(0, max(len(docs), 1), BATCH):
        last = i + BATCH >= len(docs)
        res = await kb.ingest(collection, docs[i:i + BATCH], replace=not keep, final=last)
        for k in total:
            total[k] += res[k]
    return total


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["dir", "defs", "montage"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--hash", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    import settings
    from knowledge_base import KnowledgeBase
    from search import HybridIndex, HybridSearch, SearchConfig
    from search.embedder import HashEmbedder, LmStudioEmbedder
    emb = HashEmbedder() if a.hash else LmStudioEmbedder()
    db = Path(a.db) if a.db else settings.memory_dir() / "knowledge.db"
    search = HybridSearch(HybridIndex(db), emb, None, SearchConfig())
    kb = KnowledgeBase(search)
    if a.cmd == "montage":
        collection, docs = "montage.style", montage_documents()
    else:
        if len(a.args) != 2:
            ap.error(f"{a.cmd} <коллекция> <папка>")
        collection = a.args[0]
        docs = (dir_documents if a.cmd == "dir" else defs_documents)(Path(a.args[1]))
    print(collection, await ingest(kb, collection, docs, a.keep), f"документов: {len(docs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
