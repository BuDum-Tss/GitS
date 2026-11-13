"""Роли участников шины и режим владения (контракт 1.11 добавил, 2.0 сделал поля обязательными).

Описано здесь для документа (`docs/MODULE_CONTRACT.md`); проверку ведёт `gits_sdk.protocol.validate_manifest`.
"""
from __future__ import annotations

from .. import protocol as P
from .spec import Field

# поля манифеста, связанные с ролью
MANIFEST_FIELDS = (
    Field("role", "string", True, "роль участника: workspace (окружение: инструменты, события, рабочее место) или service "
          "(подключается к ядру по WebSocket, окружения и аренды нет; инструменты допустимы)", enum=P.ROLES),
    Field("ownership", "string", False, "режим владения, обязателен у workspace и запрещён у service: shared (общий, "
          "эксклюзивный, берётся в аренду, см. раздел «Интерфейс управления») или owned (живёт внутри ядра персонажа)",
          enum=P.OWNERSHIPS),
    Field("event_prefixes", "array", False, "дополнительные префиксы видов событий (кроме id): окружение, слитое из "
          "нескольких подсистем, сохраняет прежние виды (`twitch.chat`, `obs.scene`); без поля виды начинаются с id"),
)

MANIFEST_RULES = (
    "`role` обязательно: манифест без него отвергается.",
    "`ownership` обязательно у `workspace` (`shared` или `owned`); у `service` манифест с этим полем отвергается.",
    "`event_prefixes` расширяет допустимое начало видов событий (`emits[].kind` и `client.event`): id участника и перечисленные префиксы.",
    "Поле `workspace` (описание среды агента) допустимо у `workspace` и необязательно; у `service` манифест с ним отвергается.",
)

# что в рукопожатии и окружении
HELLO_NOTES = (
    "`hello` несёт `id` и `role` (оба обязательны; `role` совпадает с ролью манифеста). Поле `module_id` не принимается; "
    "ответ ядра тоже без него.",
    f"Адрес входа единственный: `{P.WS_PATH}`.",
    f"Переменные окружения участника: `{P.ENV_ID}`, `{P.ENV_TOKEN}`, `{P.ENV_CORE_URL}`, `{P.ENV_CONTROL_TOKEN}`.",
)
