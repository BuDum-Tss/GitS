"""Дедупликация событий внимания (ТЗ 29, ARC-4): чистые решения без хранения.

Хранилище событий (`app/items.py`) спрашивает здесь, можно ли склеить новое событие со старым, и какой ключ у объявления.
"""

DEDUPE_S = 60.0            # ATT-6: окно склейки повторов с одним dedupe_key
ANNOUNCE_MERGE_S = 20.0    # AN-1: окно склейки одинаковых объявлений одного источника
STORE_MAX = 500            # сколько событий помнит хранилище
STORE_DROP = 100           # сколько самых старых выбрасывается при переполнении


def can_merge(old_exists: bool, old_spoken: bool, old_updated: float, now: float, window_s: float = DEDUPE_S) -> bool:
    """ATT-6: тот же dedupe_key за последние `window_s` секунд и старое событие ещё не озвучено."""
    return old_exists and not old_spoken and now - old_updated <= window_s


def announce_key(module: str, text: str) -> tuple[str, str]:
    """Ключ склейки объявлений: источник и текст без различий в пробелах и регистре."""
    return module, " ".join(text.split()).casefold()


def oldest_ids(created_by_id: dict[str, float], limit: int = STORE_MAX, drop: int = STORE_DROP) -> list[str]:
    """Идентификаторы самых старых событий, которые надо забыть, когда их стало больше `limit`; иначе пусто."""
    if len(created_by_id) <= limit:
        return []
    return [i for i, _ in sorted(created_by_id.items(), key=lambda kv: kv[1])[:drop]]
