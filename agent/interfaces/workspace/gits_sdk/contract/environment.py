"""Окружение и ресурсы участника шины (ТЗ 31, MC-6): какие переменные окружения передаёт супервизор, какие участник шины читает сам,
и правила папки ресурсов `resources/<Имя>/<участник шины>/`.

Супервизор (`agent/supervisor.py`, `_process_env`) обязан передавать все переменные с `by == "supervisor"`: тест
`test/test_contract_environment.py` сверяет таблицу с настоящим окружением процесса участника шины.
"""
from __future__ import annotations

from dataclasses import dataclass

SUPERVISOR = "supervisor"       # ставит супервизор при запуске каждого участника шины
USER = "user"                   # ставит человек или окружение (необязательно, есть значение по умолчанию)
INHERITED = "inherited"         # супервизор не ставит, но процесс участника шины наследует то, что задано у ядра


@dataclass(frozen=True)
class EnvVar:
    name: str
    by: str
    required: bool
    doc: str
    default: str = ""


ENV_VARS = (
    EnvVar("GITS_TOKEN", SUPERVISOR, True, "токен шины; без него SDK берёт токен из файла токена ядра"),
    EnvVar("GITS_CORE_URL", SUPERVISOR, True, "адрес шины ядра", "ws://127.0.0.1:8766/ws"),
    EnvVar("GITS_ID", SUPERVISOR, True, "id участника из peers.yaml"),
    EnvVar("MEMORY_DIR", SUPERVISOR, True, "папка данных участника шины (его память); только он пишет в неё"),
    EnvVar("GITS_CHARACTER_ID", SUPERVISOR, True, "id активного персонажа; от него зависит папка ресурсов участника шины", "glados"),
    EnvVar("PYTHONUNBUFFERED", SUPERVISOR, False, "1: журнал процесса без буфера"),
    EnvVar("PYTHONPATH", SUPERVISOR, False, "пути к sdk/ и agent/ до установки пакета gits-sdk"),
    EnvVar("GITS_CONTROL_TOKEN", USER, False, "общий токен интерфейса управления workspace с ownership shared (аренда); без него "
                                              "сервер управления не стартует"),
    EnvVar("GITS_RESOURCES_DIR", INHERITED, False, "корень ресурсов персонажей; по умолчанию resources/ рядом с sdk/"),
    EnvVar("GITS_SECRETS_FILE", INHERITED, False, "путь к .env; по умолчанию .env"),
)

# переменные, которые супервизор обязан положить в окружение процесса каждого участника шины (не сервиса)
SUPERVISOR_VARS = tuple(v.name for v in ENV_VARS if v.by == SUPERVISOR and v.required)

# правила папки ресурсов участника шины
RESOURCE_RULES = (
    "Ресурсы персонажа лежат в `resources/<Имя>/`; папка находится по `id` в `core/character.yaml`, имя папки любое.",
    "Участник шины читает ресурсы только из своей папки `resources/<Имя>/<id участника шины>/`: `gits_sdk.resources.module_dir(<id>)`.",
    "Персонаж берётся из `GITS_CHARACTER_ID`, иначе `glados`; смена персонажа перезапускает участник шины.",
    "Папка участника шины не создаётся SDK и не изменяется участником шины; изменяемые данные участника шины лежат в `MEMORY_DIR`.",
    "Корень ресурсов: `GITS_RESOURCES_DIR` или `resources/` рядом с `agent/interfaces/workspace/`.",
    "Секреты участник шины читает сам из `.env` (или `GITS_SECRETS_FILE`); в журнал и в события они не попадают.",
)
