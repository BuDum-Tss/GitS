"""Манифест модуля (ТЗ 01, раздел 6). Замените `template` на id вашего модуля (латиница, нижний регистр)."""

MODULE_ID = "template"


def build_manifest() -> dict:
    return {
        "id": MODULE_ID,
        "title": "Шаблон",
        "description": "Заготовка нового модуля: два инструмента, одно событие, типизированные запросы к ядру.",
        "version": "0.1.0",
        "subscribe": ["presence"],
        "tools": [
            {"name": "greet",
             "description": "Поздороваться от имени активного персонажа (для просьб «поздоровайся»).",
             "params": {"name": {"type": "string", "description": "кого поприветствовать"}},
             "risk": "auto", "availability": "always", "keywords": ["поздоровайся", "привет"]},
            {"name": "remember_fact",
             "description": "Запомнить факт в личной памяти ядра и вернуть то, что найдено по теме.",
             "params": {"text": {"type": "string", "description": "что запомнить"}},
             "risk": "auto", "availability": "always", "keywords": ["запомни"]},
        ],
        "emits": [{"kind": "template.greeted", "description": "Модуль поздоровался", "default_urgency": 1}],
    }
