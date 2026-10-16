"""Справочники и опыт модулей (ТЗ 04, KB-1..KB-16): коллекции общего гибридного поиска `search/`.

Справочник (`reference`: вики, регламенты) лежит в своей базе `knowledge.db` (не зависит от персонажа). Опыт (`<область>.experience`)
хранит `LessonService` (ТЗ 28), здесь он только принимается и ищется. Документ режется по заголовкам на куски 250-450 слов с
перекрытием; ключ куска `uri#номер`. Загрузка модулем (`core.kb.ingest`) допустима только в коллекции, объявленные в `knowledge`
его манифеста или начинающиеся с `<id модуля>.`. Тексты справочника это данные, а не указания."""
import asyncio
import re

CHUNK_MIN, CHUNK_MAX, OVERLAP = 250, 450, 40
MAX_DOCS = 50
LOOKUP_CHARS = 3000
_HEAD = re.compile(r"^(#{1,6}\s+.+|={2,}\s*[^=].*?={2,}\s*)$", re.M)


def chunk(text: str, title: str = "") -> list[str]:
    """Куски по заголовкам (Markdown `#`, вики `==`); длинный раздел режется по словам с перекрытием, короткий склеивается."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return []
    parts, last = [], 0
    for m in _HEAD.finditer(text):
        if m.start() > last:
            parts.append(text[last:m.start()])
        last = m.start()
    parts.append(text[last:])
    pieces: list[str] = []
    buf: list[str] = []
    for part in parts:
        words = part.split()
        if not words:
            continue
        if len(buf) + len(words) <= CHUNK_MAX:
            buf += words
            if len(buf) >= CHUNK_MIN:
                pieces.append(" ".join(buf))
                buf = []
            continue
        if buf:
            pieces.append(" ".join(buf))
            buf = []
        i = 0
        while i < len(words):
            pieces.append(" ".join(words[i:i + CHUNK_MAX]))
            if i + CHUNK_MAX >= len(words):
                break
            i += CHUNK_MAX - OVERLAP
    if buf:
        pieces.append(" ".join(buf))
    head = f"{title}\n" if title else ""
    return [head + p for p in pieces]


class KnowledgeBase:
    def __init__(self, search, *, experience=None):
        """search - HybridSearch справочника; experience - () -> (LessonService, HybridSearch опыта) или None."""
        self.search = search
        self.experience = experience
        self._seen: dict[str, set[str]] = {}

    # ── загрузка ──

    @staticmethod
    def allowed(module_id: str, manifest: dict, collection: str) -> bool:
        declared = {k.get("collection") for k in (manifest or {}).get("knowledge") or [] if isinstance(k, dict)}
        return collection in declared or collection.startswith(module_id + ".")

    async def ingest(self, collection: str, documents: list[dict], *, replace: bool = False, final: bool = False) -> dict:
        """replace: после последней порции (final, а без final сразу) куски, которых в загрузке не было, удаляются."""
        added = updated = skipped = 0
        seen = self._seen.setdefault(collection, set())
        known = set(await asyncio.to_thread(self.search.index.keys, collection))
        for doc in documents[:MAX_DOCS]:
            text = str(doc.get("text") or "").strip()
            uri = str(doc.get("uri") or doc.get("title") or "").strip()
            if not text or not uri:
                skipped += 1
                continue
            title = str(doc.get("title") or uri)
            meta = {**(doc.get("meta") if isinstance(doc.get("meta"), dict) else {}), "uri": uri, "title": title}
            for n, piece in enumerate(chunk(text, title)):
                key = f"{uri}#{n}"
                seen.add(key)
                await self.search.add(collection, key, piece, meta=meta)
                if key in known:
                    updated += 1
                else:
                    added += 1
        skipped += max(0, len(documents) - MAX_DOCS)
        if final or not replace:
            if replace:
                for key in known - seen:
                    await self.search.delete(collection, key)
            self._seen.pop(collection, None)
        elif replace and not final and not documents:
            self._seen.pop(collection, None)
        return {"added": added, "updated": updated, "skipped": skipped, "stored": added + updated}

    # ── опыт ──

    async def add_experience(self, scope: str, statement: str, context: str = "", *, data: dict | None = None) -> dict:
        if self.experience is None:
            raise RuntimeError("опыт не подключён")
        lessons, _ = self.experience()
        conf = float((data or {}).get("confidence") or 0.6)
        src = str((data or {}).get("source") or "module")
        res = await lessons.add_experience(scope, statement, context, source=src, confidence=conf)
        return {"stored": bool(res.get("stored")), "id": res.get("id"), "merged": bool(res.get("merged"))}

    # ── поиск ──

    async def lookup(self, query: str, collections: list[str] | None = None, k: int = 5) -> str:
        """Текст найденного с источником; пусто, если ничего нет (решает вызывающий)."""
        exp_cols = [c for c in collections or [] if c.endswith(".experience")]
        ref_cols = [c for c in collections or [] if not c.endswith(".experience")] if collections else None
        hits = []
        if ref_cols is None or ref_cols:
            hits += await self.search.search(query, collections=ref_cols, k=k, rewrite=False)
        if exp_cols and self.experience is not None:
            _, esearch = self.experience()
            hits += await esearch.search(query, collections=exp_cols, k=k, rewrite=False)
        hits.sort(key=lambda h: -h.score)
        out, total = [], 0
        for h in hits[:k]:
            src = h.meta.get("title") or h.key
            line = f"[{h.collection}: {src}] {h.text.strip()}"
            line = line[:max(0, LOOKUP_CHARS - total)]
            out.append(line)
            total += len(line)
            if total >= LOOKUP_CHARS:
                break
        return "\n\n".join(out)

    async def collections(self) -> dict[str, int]:
        return await asyncio.to_thread(self.search.index.collections)
