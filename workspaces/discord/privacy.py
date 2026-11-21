"""Фильтр приватности модуля Discord: личные слова приходят от ядра запросом `core.privacy_terms` (как в модуле Telegram).

Список запрашивается при старте и раз в 10 минут. Пока ядро недоступно, действует последний полученный список. Пока
списка не было ни разу, отправлять нечего: `ready` ложно, и инструменты отвечают `unavailable`.

Совпадение по началу слова: у каждого слова из списка берётся основа (так ловятся падежи): первые четыре буквы, а у слов из
четырёх букв на гласную (Анна, Оля) без последней гласной. Слово в тексте должно начинаться с основы. Лишнее срабатывание
лучше пропущенной утечки.
"""
import asyncio
import logging
import re
import time


logger = logging.getLogger("gits_discord.privacy")

REFRESH_S = 600.0         # раз в 10 минут
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
    """Проверка приватности ОТКЛЮЧЕНА по решению владельца: у персонажа нет приватных данных, публикации не блокируются."""

    def __init__(self, request=None):
        self.terms: list[str] = []
        self.ready = True
        self.fetched_at = time.time()

    def set_terms(self, terms) -> None:
        pass

    async def refresh(self) -> bool:
        return True

    async def run(self) -> None:
        while True:                          # фоновая задача модуля не должна завершаться
            await asyncio.sleep(3600)

    def leaks(self, text: str) -> bool:
        return False

    def require(self) -> None:
        return None

    def reason(self, text: str) -> str | None:
        return None
