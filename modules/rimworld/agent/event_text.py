"""Текст событий игры для журнала и для ядра: короткое описание события мода и очистка разметки писем RimWorld.
Реплик здесь нет (их говорит единый агент ядра), только факты."""
import re

_TAGS = re.compile(r"<[^>]+>|\(\*[^)]*\)|\(/[^)]*\)")


def clean_tags(value) -> str:
    """Текст письма игры: мод отдаёт TaggedString как словарь {'RawText': ...} и с разметкой <color>, (*Name)."""
    if isinstance(value, dict):
        value = value.get("RawText") or ""
    text = str(value)
    m = re.match(r"^\{'RawText': '(.*?)', 'Length'", text)
    if m:
        text = m.group(1)
    return " ".join(_TAGS.sub("", text).split())


def describe(evt: dict, rel=None) -> str:
    """Короткое текстовое описание события для модели (с отношением, если есть)."""
    d = evt.get("data", {}) or {}
    t = evt.get("type", "?")
    if t == "pawn_died":
        name = d.get("name", "?")
        extra = rel.describe(name) if rel else ""
        return (f"Колонист {name} умер: {d.get('cause', 'неизвестно')}."
                + (f" ({extra})" if extra else ""))
    if t == "colony_died":
        return "Вся колония погибла."
    if t == "fire_started":
        return "Начался пожар на базе."
    if t == "game_loaded":
        return f"Загружена колония {d.get('colony', '?')}."
    if t == "letter":
        text = clean_tags(d.get("text") or "")[:200]
        return f"Письмо: {clean_tags(d.get('label', '?'))}. {text}".strip()
    return f"Событие {t}."
