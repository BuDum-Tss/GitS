"""Роли участников шины и режим владения (контракт 1.11, фаза 1 переименования «модуль -> workspace / service»).

Описано здесь для документа (`docs/MODULE_CONTRACT.md`); проверку ведёт `gits_sdk.protocol.validate_manifest`.
"""
from __future__ import annotations

from .. import protocol as P
from .spec import Field

# поля манифеста, добавленные в 1.11
MANIFEST_FIELDS = (
    Field("role", "string", False, "роль участника: workspace (окружение: инструменты, события, рабочее место) или service "
          "(подключается к ядру по WebSocket, окружения и аренды нет; инструменты допустимы). Поля нет: workspace "
          "(режим совместимости)", enum=P.ROLES, default=P.DEFAULT_ROLE),
    Field("ownership", "string", False, "режим владения, только у workspace: shared (общий, эксклюзивный, берётся в аренду, "
          "см. раздел «Интерфейс управления») или owned (живёт внутри ядра персонажа)",
          enum=P.OWNERSHIPS, default=P.DEFAULT_OWNERSHIP),
    Field("event_prefixes", "array", False, "дополнительные префиксы видов событий (кроме id): окружение, слитое из "
          "нескольких подсистем, сохраняет прежние виды (`twitch.chat`, `obs.scene`); без поля виды начинаются с id"),
)

MANIFEST_RULES = (
    "`role` отсутствует: считается `workspace` (режим совместимости, прежние манифесты проходят без правок).",
    "`event_prefixes` расширяет допустимое начало видов событий (`emits[].kind` и `client.event`): id участника и перечисленные префиксы.",
    "`ownership` допустим только у `workspace`; у `service` манифест с этим полем отвергается.",
    "Поле `workspace` (описание среды агента) допустимо у `workspace` и необязательно; у `service` манифест с ним отвергается.",
)

# что меняется в рукопожатии
HELLO_NOTES = (
    "`hello` несёт `id` и `role`. Прежнее `module_id` клиент SDK шлёт рядом на время перехода; ядро принимает любое из двух "
    "(если заданы оба, они равны). `role`, если есть, совпадает с ролью манифеста.",
    f"Адрес входа: `{P.WS_PATH_NEW}` (новый, доступен с фазы 2) рядом с прежним `{P.WS_PATH}`. Клиент SDK пока подключается "
    f"к `{P.WS_PATH}`.",
)

# переменные окружения: новое имя читается первым, прежнее остаётся алиасом (уберёт фаза 4)
ENV_ALIASES = (
    (P.ENV_ID, P.ENV_MODULE_ID),
    (P.ENV_TOKEN, P.ENV_MODULE_TOKEN),
)
