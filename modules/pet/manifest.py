"""Манифест модуля питомца (ТЗ 13, раздел 4): только показывает, инструментов и событий нет."""
import copy

MANIFEST = {
    "id": "pet",
    "title": "Питомец",
    "description": "Питомец на рабочем столе: стикер эмоции и облачко с текущим предложением её речи, строка быстрого "
                   "ввода по горячей клавише, глушилка голоса. Звука в нём нет: озвучивает модуль voice.",
    "version": "1.0.0",
    "capabilities": ["presence.view"],
    "subscribe": ["presence", "settings", "module"],
    "shared_settings": ["voice_muted", "pet_visible", "pet_pinned", "pet_x", "pet_y", "hotkey_input", "dnd", "quiet_until"],
    "state": False,
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
