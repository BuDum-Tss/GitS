"""Манифест модуля голоса (ТЗ 12, раздел 4). Описания инструментов перенесены из прежних окружений `system` и `assistant`
(environments/builtin.py ядра) дословно: их читает планировщик."""
import copy

from common import ECHO_TAIL_DEFAULT_MS

TOOLS = [
    {"name": "recent_audio",
     "description": "Пересказать, что недавно звучало в колонках (речь в фильме, видео), если слух включён.",
     "params": {}, "risk": "auto", "availability": "always", "informative": True, "read_only": True,
     "keywords": ["слышишь", "услышала", "звучит", "говорят", "серия", "фильм", "видео"]},
]

MANIFEST = {
    "id": "voice",
    "role": "service",
    "title": "Голос",
    "description": "Звук целиком: микрофон и распознавание речи (GigaAM, запасной faster-whisper), синтез её голоса, воспроизведение "
                   "с очередью реплик, перебивание, слух системного звука (что играет в колонках).",
    "version": "1.0.0",
    "capabilities": ["voice.output", "voice.input"],
    "subscribe": ["settings", "module", "character"],
    "shared_settings": ["voice_muted", "volume", "tts_speed", "tts_fx", "mic", "hearing", "dnd", "quiet_until"],
    "settings_schema": {
        "barge_in": {"type": "boolean", "title": "Перебивание: её речь обрывается, когда говорит владелец "
                                                 "(на колонках без подавления эха возможны ложные срабатывания)",
                     "default": False},
        "echo_tail_ms": {"type": "integer", "title": "Сколько микрофон закрыт после её речи, мс",
                         "default": ECHO_TAIL_DEFAULT_MS, "min": 0, "max": 3000},
    },
    "tools": TOOLS,
    "emits": [
        {"kind": "voice.utterance", "description": "Владелец что-то сказал вслух в микрофон (`speaker.owner: true`)", "default_urgency": 3},
        {"kind": "voice.speech", "description": "Начало, сегмент, конец или обрыв её речи (служебное, не озвучивается)",
         "default_urgency": 0},
        {"kind": "voice.user_speaking", "description": "Человек начал или закончил говорить (служебное, не озвучивается)",
         "default_urgency": 0},
        {"kind": "voice.heard", "description": "Сводка услышанного в колонках", "default_urgency": 1},
        {"kind": "voice.trace", "description": "Метки звука реплики для трассы задержки (служебное, ТЗ 24)",
         "default_urgency": 0},
    ],
    "state": True,
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): микрофон, слух, говорит ли она, замеры синтеза
    "panel": {"title": "Голос", "icon": "mic", "order": 70},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
