"""Устаревший пакет-алиас: `glados_module` переименован в `gits_sdk` (ТЗ 05, CHR-3).

Оставлен, чтобы модули в других репозиториях и уже установленный старый пакет `glados-module` не сломались до миграции.
Импорт выдаёт DeprecationWarning. Пакет удаляется через одну версию (запись в CHANGELOG, раздел «Deprecated»).
"""
import sys
import warnings

import gits_sdk
from gits_sdk import *  # noqa: F403  (всё, что экспортирует gits_sdk)
from gits_sdk import client, envcompat, protocol, testing

warnings.warn("пакет glados_module переименован в gits_sdk: замените импорт (см. CHANGELOG, Deprecated)",
              DeprecationWarning, stacklevel=2)

# прежние подмодули указывают на те же объекты, что и новые: isinstance и совпадение классов сохраняются
for _name, _module in (("protocol", protocol), ("client", client), ("testing", testing), ("envcompat", envcompat)):
    sys.modules[f"{__name__}.{_name}"] = _module

__all__ = list(gits_sdk.__all__)
