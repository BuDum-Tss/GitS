"""Версии и совместимость контракта (ТЗ 31, MC-8).

`PROTOCOL` (целое, `gits_sdk.protocol`) входит в рукопожатие и поднимается только при несовместимом изменении.
`CONTRACT_VERSION` (мажор.минор) описывает состав контракта: минор растёт, когда появляются новые запросы, сообщения или
необязательные поля; мажор растёт вместе с `PROTOCOL`.

Правила:
- добавление необязательного поля, нового запроса, сообщения или темы НЕ ломает совместимость: поднимается минор;
- удаление, переименование, смена типа поля, превращение необязательного поля в обязательное поднимают `PROTOCOL`;
- элемент сначала получает статус `deprecated` с датой удаления, и только после неё может быть удалён;
- получатель игнорирует неизвестные поля и неизвестные типы сообщений (MOD-10).
"""
from __future__ import annotations

from .. import protocol as P

CONTRACT_VERSION = "1.8"
PROTOCOL = P.PROTOCOL

# версия контракта, в которой появился элемент (поле `since` у Message): "1.0" - исходный состав ТЗ 01
HISTORY = (
    ("1.0", "ТЗ 01, ТЗ 02, ТЗ 10, ТЗ 15, ТЗ 18: рукопожатие, события, речь, вопросы, задачи, панель, фокус, темы"),
    ("1.1", "ТЗ 31: core.llm.ask, core.events.tail, core.claude.locate, сообщение log; типизированный клиент"),
    ("1.2", "ТЗ 27: core.memory.episodes (эпизоды памяти ядра)"),
    ("1.3", "core.memory.recall и core.memory.remember реализованы (были planned)"),
    ("1.4", "ТЗ 04: core.lookup, core.experience.add, core.kb.ingest реализованы (были planned)"),
    ("1.5", "ТЗ 32: поле манифеста workspace, запросы workspace.enter / workspace.exit / workspace.resume "
            "(policy.activate, focus.release, focus.resume остаются алиасами на один релиз)"),
    ("1.6", "удалены справочник и уроки ядра: запросы core.lookup, core.experience.add, core.kb.ingest и поле манифеста knowledge"),
    ("1.7", "эмоций в ядре нет: стикер питомца выбирает тег в тексте ответа; presence.sticker и sticker_ts вместо emotion и emotion_ts, "
            "из speak убрано emotion, сообщение emotion и поля emotion в say, core.tts, tts.synthesize устарели (ядро их игнорирует)"),
    ("1.8", "ТЗ 33: рабочее место. Поля манифеста nodes (дерево узлов, context узла, watch), tools[].node, у emits поля "
            "urgency_unfocused, topics, follow_up; поле node в сообщении state. Правила: docs/WORKSPACE_MODULES.md"),
)

# Устаревшее: (элемент, дата удаления, чем заменить)
DEPRECATED: tuple[tuple[str, str, str], ...] = (
    ("emotion", "2027-01-01", "тег стикера в тексте ответа; сообщение игнорируется ядром"),
)
