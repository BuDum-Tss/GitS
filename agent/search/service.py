"""Гибридный поиск как сервис: индекс + эмбеддер (контракт `Embedder`) + переписывание запроса (контракт `LanguageModel`).

Все блокирующие обращения к SQLite уходят в `asyncio.to_thread`. Сбой эмбеддера не роняет поиск: остаётся FTS."""
import asyncio
import logging

from contracts import Embedder, LanguageModel
from search.index import Hit, HybridIndex, SearchConfig
from search.rewrite import rewrite_query

logger = logging.getLogger("hybrid_search")


class HybridSearch:
    def __init__(self, index: HybridIndex, embedder: Embedder | None = None, llm: LanguageModel | None = None,
                 cfg: SearchConfig | None = None):
        self.index = index
        self.embedder = embedder
        self.llm = llm
        self.cfg = cfg or SearchConfig()
        self.last_embedded = False       # у последней записи `add` есть вектор (эмбеддер был доступен); нужно эпизодам (ТЗ 27)

    async def _embed(self, texts: list[str], query: bool) -> list[list[float]] | None:
        if self.embedder is None:
            return None
        try:
            return await self.embedder.embed(texts, query)
        except Exception as e:
            logger.info(f"Эмбеддер недоступен, поиск только по словам: {e}")
            return None

    async def add(self, collection: str, key: str, text: str, *, ts: float | None = None, meta: dict | None = None,
                  supersedes: list[str] | None = None) -> int:
        vecs = await self._embed([text], False)
        self.last_embedded = bool(vecs)
        return await asyncio.to_thread(self.index.upsert, collection, key, text, vec=vecs[0] if vecs else None, ts=ts,
                                       meta=meta, supersedes=supersedes)

    async def delete(self, collection: str, key: str) -> bool:
        return await asyncio.to_thread(self.index.delete, collection, key)

    async def search(self, query: str, *, tail: list[str] | None = None, collections: list[str] | None = None,
                     k: int = 5, since: float | None = None, until: float | None = None, meta: dict | None = None,
                     include_stale: bool = False, rewrite: bool = True) -> list[Hit]:
        q = await rewrite_query(self.llm, query, tail) if rewrite else query
        vecs = await self._embed([q], True)
        return await asyncio.to_thread(
            self.index.search, q, vecs[0] if vecs else None, collections=collections, k=k, since=since, until=until,
            meta=meta, include_stale=include_stale, cfg=self.cfg)
