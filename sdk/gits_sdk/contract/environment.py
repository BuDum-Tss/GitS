"""Окружение и ресурсы модуля (ТЗ 31, MC-6): какие переменные окружения передаёт супервизор, какие модуль читает сам,
и правила папки ресурсов `resources/<Имя>/<модуль>/`.

Супервизор (`agent/supervisor.py`, `_process_env`) обязан передавать все переменные с `by == "supervisor"`: тест
`agent/tests/test_contract_environment.py` сверяет таблицу с настоящим окружением процесса модуля.
"""
from __future__ import annotations

from dataclasses import dataclass

SUPERVISOR = "supervisor"       # ставит супервизор при запуске каждого модуля
USER = "user"                   # ставит человек или окружение (необязательно, есть значение по умолчанию)
INHERITED = "inherited"         # супервизор не ставит, но процесс модуля наследует то, что задано у ядра


@dataclass(frozen=True)
class EnvVar:
    name: str
    by: str
    required: bool
    doc: str
    default: str = ""


ENV_VARS = (
    EnvVar("GITS_MODULE_TOKEN", SUPERVISOR, True, "токен шины модулей; без него SDK берёт токен из файла токена ядра"),
    EnvVar("GITS_CORE_URL", SUPERVISOR, True, "адрес шины ядра", "ws://127.0.0.1:8766/module/ws"),
    EnvVar("GITS_MODULE_ID", SUPERVISOR, True, "id модуля из modules.yaml"),
    EnvVar("MEMORY_DIR", SUPERVISOR, True, "папка данных модуля (его память); только он пишет в неё"),
    EnvVar("GITS_CHARACTER_ID", SUPERVISOR, True, "id активного персонажа; от него зависит папка ресурсов модуля", "glados"),
    EnvVar("PYTHONUNBUFFERED", SUPERVISOR, False, "1: журнал процесса без буфера"),
    EnvVar("PYTHONPATH", SUPERVISOR, False, "пути к sdk/ и agent/ до установки пакета gits-sdk"),
    EnvVar("GITS_RESOURCES_DIR", INHERITED, False, "корень ресурсов персонажей; по умолчанию resources/ рядом с sdk/"),
    EnvVar("GITS_SECRETS_FILE", INHERITED, False, "путь к secrets.env; по умолчанию agent/secrets.env"),
)

# переменные, которые супервизор обязан положить в окружение процесса каждого модуля (не сервиса)
SUPERVISOR_VARS = tuple(v.name for v in ENV_VARS if v.by == SUPERVISOR and v.required)

# правила папки ресурсов модуля
RESOURCE_RULES = (
    "Ресурсы персонажа лежат в `resources/<Имя>/`; папка находится по `id` в `core/character.yaml`, имя папки любое.",
    "Модуль читает ресурсы только из своей папки `resources/<Имя>/<id модуля>/`: `gits_sdk.resources.module_dir(<id>)`.",
    "Персонаж берётся из `GITS_CHARACTER_ID`, иначе `glados`; смена персонажа перезапускает модуль.",
    "Папка модуля не создаётся SDK и не изменяется модулем; изменяемые данные модуля лежат в `MEMORY_DIR`.",
    "Корень ресурсов: `GITS_RESOURCES_DIR` или `resources/` рядом с `sdk/`.",
    "Секреты модуль читает сам из `agent/secrets.env` (или `GITS_SECRETS_FILE`); в журнал и в события они не попадают.",
)
