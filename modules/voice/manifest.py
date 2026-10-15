"""Манифест модуля голоса (ТЗ 12, раздел 4). Описания инструментов перенесены из прежних окружений `system` и `assistant`
(environments/builtin.py ядра) дословно: их читает планировщик."""
import copy

from common import ECHO_TAIL_DEFAULT_MS

TOOLS = [
    {"name": "recent_audio",
     "description": "Пересказать, что недавно звучало в колонках (речь в фильме, видео), если слух включён.",
     "params": {}, "risk": "auto", "availability": "always", "informative": True,
     "keywords": ["слышишь", "услышала", "звучит", "говорят", "серия", "фильм", "видео"]},
    {"name": "name_voice",
     "description": "Запомнить, чей это голос: тот, что говорил только что (через микрофон). "
                    "is_owner=true, если это сам пользователь («запомни мой голос, я Аня»). Для просьб «запомни мой голос», "
                    "«это голос Васи».",
     "params": {"name": {"type": "string", "description": "имя человека"},
                "is_owner": {"type": "boolean", "required": False, "description": "true: это сам пользователь"}},
     "risk": "auto", "availability": "always",
     "keywords": ["голос", "запомни", "узнавай", "это", "меня", "зовут", "микрофон"]},
]

MANIFEST = {
    "id": "voice",
    "title": "Голос",
    "description": "Звук целиком: микрофон и распознавание речи, узнавание голосов, синтез её голоса, воспроизведение "
                   "с очередью реплик, перебивание, слух системного звука (что играет в колонках).",
    "version": "1.0.0",
    "capabilities": ["voice.output", "voice.input"],
    "subscribe": ["settings", "module", "character"],
    "shared_settings": ["voice_muted", "volume", "mic", "hearing", "dnd", "quiet_until"],
    "settings_schema": {
        "barge_in": {"type": "boolean", "title": "Перебивание: её речь обрывается, когда говорит владелец "
                                                 "(на колонках без подавления эха возможны ложные срабатывания)",
                     "default": False},
        "echo_tail_ms": {"type": "integer", "title": "Сколько микрофон закрыт после её речи, мс",
                         "default": ECHO_TAIL_DEFAULT_MS, "min": 0, "max": 3000},
    },
    "tools": TOOLS,
    "emits": [
        {"kind": "voice.utterance", "description": "Пользователь или другой человек что-то сказал вслух", "default_urgency": 3},
        {"kind": "voice.speech", "description": "Начало, сегмент, конец или обрыв её речи (служебное, не озвучивается)",
         "default_urgency": 0},
        {"kind": "voice.user_speaking", "description": "Человек начал или закончил говорить (служебное, не озвучивается)",
         "default_urgency": 0},
        {"kind": "voice.heard", "description": "Сводка услышанного в колонках", "default_urgency": 1},
        {"kind": "voice.named", "description": "Голосу назначено имя (служебное: ядро заводит человека в памяти)",
         "default_urgency": 0},
    ],
    "state": True,
    # политики фокуса (ТЗ 18, FOC-1; ТЗ 11e, POL-13): голос работает фоном, фокус не держит
    "policies": [
        {"id": "ambient", "title": "Фон", "focus": "background", "interruptible": True,
         "purpose": "микрофон и речь в фоне, фокус не держит"},
    ],
    "default_policy": "ambient",
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): микрофон, слух, говорит ли она, замеры синтеза
    "panel": {"title": "Голос", "icon": "mic", "order": 70},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
