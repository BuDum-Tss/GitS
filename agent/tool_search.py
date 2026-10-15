"""Отбор инструментов по смыслу (ТЗ 02, CORE-36, ЖЕЛАТЕЛЬНО): эмбеддинги описаний через EmbeddingGemma в LM Studio.

Описание каждого инструмента (имя, описание, ключевые слова, окружение) превращается в вектор; кэш лежит в
`memory/tool_embeddings.json` по хешу описания, поэтому пересчитываются только изменившиеся. Просьба пользователя
превращается в вектор запроса, инструменты ранжируются по косинусу, берутся первые k. Планировщик видит только их.

Это ускоритель, а не обязательная часть: если LM Studio или модель эмбеддингов недоступны, кэш ещё не собран или вызов
не уложился в тайм-аут, `rank` возвращает None и реестр отбирает инструменты по основам слов, как раньше. После сбоя
эмбеддинги отключаются на минуту, чтобы каждая просьба не ждала тайм-аута. Нехватка векторов докачивается в фоне.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from envcompat import env

logger = logging.getLogger("tool_search")

LM_URL = env("GITS_LM_URL", "http://127.0.0.1:1234/v1")
EMB_MODEL = env("GITS_GRAPH_EMBEDDER", "text-embedding-google_embeddinggemma-300m-qat")
QUERY_TIMEOUT_S = 2.0
BATCH_TIMEOUT_S = 60.0
BREAKER_S = 60.0

EmbedFn = Callable[[list[str], bool], Awaitable[list[list[float]]]]    # (тексты, это запрос?) -> векторы


async def lm_studio_embed(texts: list[str], query: bool, timeout: float = BATCH_TIMEOUT_S) -> list[list[float]]:
    """Эмбеддинги EmbeddingGemma из LM Studio (приставки запроса и документа, как в graph_memory)."""
    prep = (lambda t: f"task: search result | query: {t}") if query else (lambda t: f"title: none | text: {t}")
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(f"{LM_URL}/embeddings", json={"model": EMB_MODEL, "input": [prep(t) for t in texts]})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def tool_doc(tool, env) -> str:
    """Текст, по которому ищется инструмент."""
    return f"{tool.name}. {tool.description} {' '.join(tool.keywords)} {env.title}. {env.description}".strip()


def _key(doc: str) -> str:
    return hashlib.sha1(f"{EMB_MODEL}|{doc}".encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class ToolEmbeddings:
    def __init__(self, cache_path: Path | str | None = None, embed: EmbedFn | None = None,
                 clock: Callable[[], float] = time.monotonic):
        base = os.getenv("MEMORY_DIR") or "./memory"
        self.path = Path(cache_path) if cache_path else Path(base) / "tool_embeddings.json"
        self._embed = embed or (lambda texts, query: lm_studio_embed(texts, query, QUERY_TIMEOUT_S if query else BATCH_TIMEOUT_S))
        self._clock = clock
        self._cache: dict[str, list[float]] | None = None
        self._down_until = 0.0
        self._filling: asyncio.Task | None = None

    # ── кэш ─────────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, list[float]]:
        if self._cache is None:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self._cache or {}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning(f"Кэш эмбеддингов инструментов не записан: {e}")

    # ── докачка недостающих векторов ───────────────────────────────────────────

    async def _fill(self, docs: list[str]) -> None:
        cache = self._load()
        missing = [d for d in dict.fromkeys(docs) if _key(d) not in cache]
        try:
            for i in range(0, len(missing), 32):
                chunk = missing[i:i + 32]
                vectors = await self._embed(chunk, False)
                for doc, vec in zip(chunk, vectors, strict=True):
                    cache[_key(doc)] = vec
            if missing:
                await asyncio.to_thread(self._save)
        except Exception as e:
            self._down_until = self._clock() + BREAKER_S
            logger.info(f"Эмбеддинги инструментов недоступны, отбор по словам: {e}")

    # ── ранжирование ────────────────────────────────────────────────────────

    async def rank(self, query: str, tools: list, envs: dict, k: int) -> list | None:
        """Первые k инструментов по смыслу или None (недоступно, кэш не готов, тайм-аут): тогда отбор по словам."""
        if self._clock() < self._down_until or not query.strip():
            return None
        docs = {t.id: tool_doc(t, envs[t.env]) for t in tools}
        cache = self._load()
        if any(_key(d) not in cache for d in docs.values()):
            if self._filling is None or self._filling.done():
                self._filling = asyncio.get_running_loop().create_task(self._fill(list(docs.values())))
            return None
        try:
            qv = (await asyncio.wait_for(self._embed([query], True), QUERY_TIMEOUT_S))[0]
        except Exception as e:
            self._down_until = self._clock() + BREAKER_S
            logger.info(f"Эмбеддинг просьбы не получен, отбор по словам: {e}")
            return None
        scored = sorted(tools, key=lambda t: -_cosine(qv, cache[_key(docs[t.id])]))
        return scored[:k]
