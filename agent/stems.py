"""Основы слов для сравнения текстов по пересечению (повторы реплик, воспоминания о себе, «забудь …»).

Без хранилищ и состояния: имя и алиасы активного персонажа входят в стоп-слова (его имя не бывает темой).
"""
import re

_WORD = re.compile(r"[а-яёa-z0-9]{3,}", re.I)
_STOP = {"что", "как", "это", "про", "мне", "меня", "или", "для", "все", "всё", "обо", "мой", "моя",
         "его", "ему", "она", "они", "при", "над", "под", "без", "через", "тоже", "когда", "если",
         "пользователь", "пользователя", "пользователю", "ассистентка"}


def stop_words() -> set[str]:
    """Служебные слова и имя с алиасами активного персонажа (ТЗ 05, CHR-4)."""
    import character
    try:
        return _STOP | {a.lower() for a in character.active().aliases}
    except Exception:
        return _STOP


def _stem(w: str) -> str:
    """Основа слова: отрезаем до двух букв (окончание), но не короче 4 и не длиннее 6 букв.
    Короткие четыре буквы склеивали разные слова («посетить» и «поселенцев»)."""
    w = w.lower()
    return w[:min(6, max(4, len(w) - 2))]


def stems(text: str) -> set[str]:
    stop = stop_words()
    return {_stem(w) for w in _WORD.findall(text) if w.lower() not in stop}


def words(text: str) -> list[str]:
    """Значимые слова текста (без служебных и имени персонажа), в нижнем регистре."""
    stop = stop_words()
    return [w.lower() for w in _WORD.findall(text) if w.lower() not in stop]
