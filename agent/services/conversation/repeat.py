"""Повтор реплики: похожа ли новая фраза на то, что она уже говорила недавно (чистые функции).

Небольшая модель, получив похожие входные данные и свои же прошлые реплики в контексте, повторяет их почти дословно («Нажата
кнопка. Видишь? Я просто повторяю»). Сравнение по общим основам слов (первые 5 букв) и по доле общих слов: достаточно, чтобы
поймать перефразированный повтор, и не нужны ни модель, ни эмбеддинги."""
import re

_WORD = re.compile(r"[a-zа-яё0-9]+")
STEM = 5
SIMILAR = 0.6            # доля общих основ от меньшей из двух фраз, с которой реплика считается повтором
MIN_WORDS = 3            # короткие фразы («Хорошо.») повтором не считаем


def stems(text: str) -> set[str]:
    return {w[:STEM] for w in _WORD.findall((text or "").lower().replace("ё", "е")) if len(w) >= 3}


def is_repeat(candidate: str, recent: list[str], threshold: float = SIMILAR) -> bool:
    """True, если кандидат почти повторяет одну из недавних реплик."""
    new = stems(candidate)
    if len(new) < MIN_WORDS:
        return False
    for old in (stems(t) for t in recent):
        if len(old) >= MIN_WORDS and len(new & old) / min(len(new), len(old)) >= threshold:
            return True
    return False
