"""Срок плана по словам в тексте («завтра», «в субботу»): чистая функция, без часов и ввода-вывода.

План, записанный без точного срока, получает его из текста; если слов-подсказок нет, план устаревает через
`UNDATED_DAYS` дней, иначе он не исчезал бы никогда.
"""
from datetime import datetime, timedelta

_WEEKDAYS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4),
             ("суббот", 5), ("воскресень", 6)]
UNDATED_DAYS = 14


def end_of_day(ts: float, plus_days: int) -> float:
    d = datetime.fromtimestamp(ts) + timedelta(days=plus_days)
    return d.replace(hour=23, minute=59, second=0, microsecond=0).timestamp()


def due_of_text(text: str, ts: float) -> float:
    """Срок плана (эпоха), записанного в момент `ts`: по словам в тексте, иначе через `UNDATED_DAYS` дней."""
    low = text.lower()
    if "послезавтра" in low:
        return end_of_day(ts, 2)
    if "завтра" in low:
        return end_of_day(ts, 1)
    if "сегодня" in low:
        return end_of_day(ts, 0)
    for name, wd in _WEEKDAYS:
        if name in low:
            ahead = (wd - datetime.fromtimestamp(ts).weekday()) % 7 or 7
            return end_of_day(ts, ahead)
    return ts + UNDATED_DAYS * 86400
