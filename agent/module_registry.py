"""Временный псевдоним старого имени `module_registry` (фаза 2a переименования): реестр участников теперь `peer_registry`.

Нужен, пока тест `modules/telegram/tests/test_tg_with_real_core.py` импортирует `ModuleRegistry` (папку `modules/` переносит
фаза 3); в ядре никто не импортирует этот файл. Уберёт фаза 4 вместе с остальными алиасами.
"""
from peer_registry import PeerRecord, PeerRegistry  # noqa: F401

ModuleRegistry = PeerRegistry
ModuleRecord = PeerRecord
