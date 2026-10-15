"""Переменные окружения: новое имя с запасным старым GLADOS_* (ТЗ 05, CHR-1, CHR-2).

`env("GITS_CORE_URL", default)` читает GITS_CORE_URL. Если его нет, а задано старое GLADOS_CORE_URL, берётся оно, и в журнал
один раз за процесс пишется предупреждение с обоими именами (значение не пишется). Старые имена удаляются через одну версию:
запись в CHANGELOG, раздел «Deprecated».

Это тот же файл, что копии в `modules/<id>/envcompat.py`, `sdk/gits_sdk/envcompat.py` и `services/decider/envcompat.py`
(модули и SDK не импортируют `agent/`). Тест `agent/tests/test_envcompat.py` сверяет копии: правь все вместе.
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("gits.env")

# имена, у которых хвост не совпадает с прежним GLADOS_<хвост> (таблица ТЗ 05, раздел 2)
EXPLICIT = {"GITS_CORE_HTTP_URL": "GLADOS_ASSISTANT_URL"}

_warned: set[str] = set()
_lock = threading.Lock()


def legacy_name(name: str) -> str | None:
    """Старое имя для нового: GITS_X и CHARACTER_X -> GLADOS_X (кроме явных исключений)."""
    if name in EXPLICIT:
        return EXPLICIT[name]
    for prefix in ("GITS_", "CHARACTER_"):
        if name.startswith(prefix):
            return "GLADOS_" + name[len(prefix):]
    return None


def env(name: str, default: str | None = None) -> str | None:
    """Значение переменной окружения; при отсутствии нового имени — старое GLADOS_* с однократным предупреждением."""
    if name in os.environ:
        return os.environ[name]
    old = legacy_name(name)
    if old and old in os.environ:
        with _lock:
            first = old not in _warned
            _warned.add(old)
        if first:
            log.warning("переменная %s устарела, переименуйте в %s (старое имя пока читается)", old, name)
        return os.environ[old]
    return default
