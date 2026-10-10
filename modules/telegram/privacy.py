"""Фильтр приватности модуля Telegram (ТЗ 10, TG-5): личные слова приходят от ядра запросом `core.privacy_terms`.

Список запрашивается при старте и раз в 10 минут. Пока ядро недоступно, действует последний полученный список. Пока
списка не было ни разу, публиковать и отправлять нечего: `ready` ложно, и инструменты отвечают `unavailable`.

Совпадение по началу слова: у каждого слова из списка берётся основа (так ловятся падежи): первые четыре буквы, а у слов из
четырёх букв на гласную (Анна, Оля) без последней гласной. Слово в тексте должно начинаться с основы: «Аня» не срабатывает на
«баня», а «Анне» и «Анны» срабатывают на «Анна». Лишнее срабатывание лучше пропущенной утечки.
"""
import asyncio
import logging
import re
import time

from common import scrub
from glados_module import ToolError

import events_log

logger = logging.getLogger("telegram.privacy")

REFRESH_S = 600.0         # TG-5: раз в 10 минут
RETRY_S = 15.0            # пока списка нет совсем: пробуем чаще
PREFIX = 4
_VOWELS = "аяеоуыэюи"
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё\-]{3,}")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def stem(word: str) -> str:
    """Основа слова для поиска по началу: падежи имён не должны обходить фильтр."""
    w = _norm(word)
    if len(w) == PREFIX and w[-1] in _VOWELS and len(w) - 1 >= 3:
        return w[:-1]
    return w[:PREFIX]


class Privacy:
    def __init__(self, request):
        """request - `client.request` (async (type, body, timeout=...) -> value)."""
        self._request = request
        self.terms: list[str] = []
        self.ready = False
        self.fetched_at = 0.0
        self._patterns: list[re.Pattern] = []

    def set_terms(self, terms) -> None:
        """Принять список слов от ядра (и пересобрать шаблоны поиска)."""
        clean = [str(t).strip() for t in (terms or []) if isinstance(t, str) and str(t).strip()]
        patterns, seen = [], set()
        for term in clean:
            for word in _WORD.findall(term):
                prefix = stem(word)
                if prefix not in seen:
                    seen.add(prefix)
                    patterns.append(re.compile(r"(?<!\w)" + re.escape(prefix)))
        self.terms, self._patterns = clean, patterns
        self.ready, self.fetched_at = True, time.time()

    async def refresh(self) -> bool:
        """Запросить список у ядра. При ошибке остаётся прежний (или его нет)."""
        try:
            value = await self._request("core.privacy_terms", {}, timeout=20)
            terms = value.get("terms") if isinstance(value, dict) else None
            if not isinstance(terms, list):
                raise ValueError("ядро вернуло список личных слов в неверной форме")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            events_log.log_nowait("telegram", scrub(f"личные слова от ядра не получены: {e}"))
            logger.warning("личные слова от ядра не получены: %s", e)
            return False
        self.set_terms(terms)
        return True

    async def run(self) -> None:
        while True:
            ok = await self.refresh()
            await asyncio.sleep(REFRESH_S if (ok or self.ready) else RETRY_S)

    def leaks(self, text: str) -> bool:
        """Есть ли в тексте личное слово. Пока список не получен, это не проверка: решает `ready`."""
        low = _norm(text)
        return any(p.search(low) for p in self._patterns)

    def require(self) -> None:
        """TG-5: без списка личных слов от ядра ничего публичного и ничего чужим не отправляем (unavailable)."""
        if not self.ready:
            raise ToolError("список личных слов от ядра ещё не получен: публиковать и отправлять пока нельзя",
                            code="unavailable")

    def reason(self, text: str) -> str | None:
        """Причина для tool.check: слова не называются, чтобы не повторять личное."""
        return "в тексте есть личные слова владельца или знакомых (имя и подобное): такое не отправляю" \
            if self.leaks(text) else None
