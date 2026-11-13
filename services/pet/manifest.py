"""Манифест модуля питомца (ТЗ 13, раздел 4): только показывает, инструментов и событий нет."""
import copy

MANIFEST = {
    "id": "pet",
    "role": "service",
    "title": "Питомец",
    "description": "Питомец на рабочем столе: стикер эмоции и облачко с текущим предложением её речи, строка быстрого "
                   "ввода по горячей клавише, глушилка голоса. Звука в нём нет: озвучивает модуль voice.",
    "version": "1.0.0",
    "capabilities": ["presence.view"],
    "subscribe": ["presence", "settings", "module"],
    "shared_settings": ["voice_muted", "dnd", "quiet_until"],           # общие настройки ядра, которые он только показывает
    # собственные настройки питомца (хранятся в peers.pet, ядро их не знает): окно, облачко, клавиша строки ввода
    "settings_schema": {
        "visible": {"type": "boolean", "title": "Показывать питомца", "default": True},
        "pinned": {"type": "boolean", "title": "Закрепить питомца (нельзя сдвинуть мышью)", "default": False},
        "bubble": {"type": "boolean", "title": "Облачко с текстом речи", "default": True},
        "bubble_secs": {"type": "integer", "title": "Сколько секунд держится облачко", "default": 25, "min": 3, "max": 120},
        "hotkey_input": {"type": "string", "title": "Клавиша строки быстрого ввода", "default": "Ctrl+Shift+Space",
                         "enum": ["Ctrl+Shift+Space", "Alt+Shift+Space", "Ctrl+Alt+A", "Ctrl+Alt+Q"]},
        "x": {"type": "integer", "title": "Позиция окна по горизонтали (-1: угол по умолчанию)", "default": -1},
        "y": {"type": "integer", "title": "Позиция окна по вертикали (-1: угол по умолчанию)", "default": -1},
    },
    "state": False,
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): окно, стикер, позиция, связь и кнопки «Показать» / «Скрыть»
    # (меняют собственную настройку visible через дашборд, TZ 15b)
    "panel": {"title": "Питомец", "icon": "smile", "order": 80, "actions": ["show", "hide"]},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
