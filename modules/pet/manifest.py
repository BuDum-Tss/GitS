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
    # политики фокуса (ТЗ 18, FOC-1; ТЗ 11e, POL-13): питомец только показывает, фокус не держит
    "policies": [
        {"id": "ambient", "title": "Фон", "focus": "background", "interruptible": True,
         "purpose": "стикер и облачко без управления"},
    ],
    "default_policy": "ambient",
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): окно, стикер, позиция, связь и кнопки «Показать» / «Скрыть»
    # (через core.set_setting pet_visible, TZ 15b)
    "panel": {"title": "Питомец", "icon": "smile", "order": 80, "actions": ["show", "hide"]},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
