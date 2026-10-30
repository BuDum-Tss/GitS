"""Политики модулей-заглушек для тестов сред и ModuleLink."""

GAME_POLICIES = {
    "default_policy": "afk",
    "policies": [
        {"id": "afk", "title": "Вне игры", "focus": "background", "interruptible": True, "transitions": []},
        {"id": "observe", "title": "Наблюдение", "focus": "active", "interruptible": True, "transitions": []},
        {"id": "combat", "title": "Бой", "focus": "active", "interruptible": True, "transitions": []},
    ],
}

TELEGRAM_POLICIES = {
    "default_policy": "available",
    "policies": [
        {"id": "available", "title": "Доступна", "focus": "background", "transitions": ["do_not_disturb", "replying"]},
        {"id": "do_not_disturb", "title": "Не беспокоить", "focus": "background", "quiet": True,
         "transitions": ["available"]},
        {"id": "replying", "title": "Отвечает", "focus": "active", "transitions": ["available"],
         "interruptible": True},
    ],
}

MONTAGE_POLICIES = {
    "default_policy": "idle",
    "policies": [
        {"id": "idle", "title": "Простой", "focus": "background", "transitions": []},
        {"id": "working", "title": "Монтаж", "focus": "active", "transitions": [], "interruptible": True},
        {"id": "waiting_for_materials", "title": "Нет исходников", "focus": "background", "transitions": []},
    ],
}
