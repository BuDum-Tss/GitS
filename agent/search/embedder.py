"""Эмбеддеры для гибридного поиска (контракт `contracts.Embedder`).

`LmStudioEmbedder` переиспользует вызов EmbeddingGemma из `tool_search.py` (приставки запроса и документа те же).
`HashEmbedder` детерминированный и без сети: хеширование основ слов и пар слов в вектор. Нужен тестам и стенду
`tools/memory_bakeoff.py`; смысла за пределами совпадения слов он не знает."""
import hashlib
import math

from search.normalize import tokens


class HashEmbedder:
    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = tokens(text)
        feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:], strict=False)]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "big")
            v[h % self.dim] += 1.0 if (h >> 40) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v] if n else v

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class LmStudioEmbedder:
    """EmbeddingGemma в LM Studio; тайм-аут запроса короткий (поиск в разговоре), документов длинный."""

    def __init__(self, query_timeout: float = 2.0, batch_timeout: float = 60.0):
        self.query_timeout = query_timeout
        self.batch_timeout = batch_timeout

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]:
        import tool_search
        return await tool_search.lm_studio_embed(texts, query, self.query_timeout if query else self.batch_timeout)
