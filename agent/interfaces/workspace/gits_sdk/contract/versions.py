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

CONTRACT_VERSION = "3.3"
PROTOCOL = P.PROTOCOL

# версия контракта, в которой появился элемент (поле `since` у Message): "1.0" - исходный состав ТЗ 01
HISTORY = (
    ("1.0", "ТЗ 01, ТЗ 02, ТЗ 10, ТЗ 15, ТЗ 18: рукопожатие, события, речь, вопросы, задачи, панель, фокус, темы"),
    ("1.1", "ТЗ 31: core.llm.ask, core.events.tail, core.claude.locate, сообщение log; типизированный клиент"),
    ("1.2", "ТЗ 27: core.memory.episodes (эпизоды памяти ядра)"),
    ("1.3", "core.memory.recall и core.memory.remember реализованы (были planned)"),
    ("1.4", "ТЗ 04: core.lookup, core.experience.add, core.kb.ingest реализованы (были planned)"),
    ("1.5", "ТЗ 32: поле манифеста workspace, запросы workspace.enter / workspace.exit / workspace.resume "
            "(старые имена были алиасами, удалены в 1.10)"),
    ("1.6", "удалены справочник и уроки ядра: запросы core.lookup, core.experience.add, core.kb.ingest и поле манифеста knowledge"),
    ("1.7", "эмоций в ядре нет: стикер питомца выбирает тег в тексте ответа; presence.sticker и sticker_ts вместо emotion и emotion_ts, "
            "из speak убрано emotion, сообщение emotion и поля emotion в say, core.tts, tts.synthesize устарели (ядро их игнорирует)"),
    ("1.8", "ТЗ 33: рабочее место. Поля манифеста nodes (дерево узлов, context узла, watch), tools[].node, у emits поля "
            "urgency_unfocused, topics, follow_up; поле node в сообщении state; поля среды workspace.autonomous и work_interval_s "
            "(рабочий цикл агента). Правила: docs/WORKSPACE_MODULES.md"),
    ("1.9", "ТЗ 32: core.tasks (цель и открытые задачи среды для панели или оверлея модуля)"),
    ("1.10", "удалены политики модуля: поля policies и default_policy, запросы policy.activate, focus.release, focus.resume, "
             "параметр policy у workspace.*, active_policy в статусе; конец состояния «задача кончилась» сообщает модуль "
             "флагом done в статусе"),
    ("1.11", "фаза 1 переименования «модуль -> workspace / service» (аддитивно): поля манифеста role и ownership, поля id и role "
             "в hello (прежнее module_id шлётся рядом), адрес входа /ws рядом с /module/ws (доступен с фазы 2), переменные "
             "GITS_ID и GITS_TOKEN (GITS_MODULE_ID и GITS_MODULE_TOKEN остаются алиасами), HTTP-интерфейс управления "
             "workspace с ownership shared: аренда без срока и продления (GET /status, GET /info, POST /acquire, /release; "
             "аренда кончается по release или при обрыве WebSocket участника), поля среды workspace.when_to_use и keywords"),
    ("2.0", "фаза 4 переименования, PROTOCOL 2: удалены алиасы фаз 1-3 (ModuleClient, module_id в hello и в ответе ядра, адрес "
            "/module/ws, переменные GITS_MODULE_ID и GITS_MODULE_TOKEN, раздел modules: в peers.yaml, /api/modules); role в "
            "манифесте и hello и ownership у workspace обязательны (без них reject). Запросы workspace.enter, workspace.exit, "
            "workspace.resume без изменений"),
    ("3.0", "PROTOCOL 3, только pi: удалены запросы core.claude_usage и core.claude.locate, тема battery и поле battery в welcome "
            "(батарейки Claude нет), клиентские методы claude_usage, claude_locate, поле client.battery и модуль claude_path"),
    ("3.1", "core.tts и tts.synthesize: необязательное поле volume_path в ответе (файл относительно папки персонажа: участники "
            "в контейнерах обмениваются файлами через общий том персонажа, `shared/tts/`)"),
    ("3.2", "рабочее окружение агента (docs/WORKSPACE_CONTRACT.md): необязательные поля блока workspace манифеста brief, skills, "
            "area, commands, site (неверные поля не отклоняют модуль, а попадают в предупреждения); запрос workspace.afk (выход "
            "без отпускания аренды, тело как у workspace.exit); ответ на workspace.enter может нести brief, skills, units, site"),
    ("3.3", "tool.call: необязательная типизированная форма ответа `{text, delivered_to}` (SDK: `ToolReply`); `delivered_to` - адреса "
            "(формат reply_channel.target, до 8 по 200 символов), куда инструмент реально доставил сообщение: ядро не дублирует "
            "туда итоговую реплику хода"),
)

# Устаревшее: (элемент, дата удаления, чем заменить)
DEPRECATED: tuple[tuple[str, str, str], ...] = (
    ("emotion", "2027-01-01", "тег стикера в тексте ответа; сообщение игнорируется ядром"),
)
