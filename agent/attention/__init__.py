"""Сервис внимания событий (ТЗ 03): что из происходящего заслуживает её слов, когда и насколько срочно.

Отдельный сервис внутри процесса ядра: всё, что относится к приёму события (правила R1-R4, срочность, решение D0-D4, склейка
повторов, входящая очередь, очередь речи, временные правила), лежит в этой папке. Остальное ядро обращается к нему через
`attention.Attention` (протокол `contracts.EventAttention`) и не импортирует внутренние файлы папки.
"""
from .core import (  # noqa: F401  (имена прежнего модуля attention.py: их импортируют ядро и тесты)
    ANNOUNCE_DEFAULT_URGENCY,
    CONVERSATION_FOCUS_P,
    CONVERSATION_P,
    FOCUS_DEPTH,
    PROACTIVE_P,
    QUESTION_ANNOUNCE_P,
    RULES_PURGE_S,
    SPEECH_DEFAULT_URGENCY,
    TEXT_LOG,
    TEXT_MAX,
    TICK_S,
    AttentionContext,
    Focus,
    Item,
    NullPresence,
    NullSpeech,
    Question,
    clamp,
)
from .service import Attention  # noqa: F401
