"""Сервис памяти (ТЗ 04, 27, 29): всё, что персонаж помнит.

Граф фактов (`graph.py`, `fact_store.py`, области `scope.py`), диалог и активность (`user.py`), эпизоды, сон и дневник
(`episodes.py`, `episode_*.py`, `sleep*.py`, `diary.py`), явное «запомни» (`capture.py`), адаптеры контрактов `MemoryReader` и
`MemoryWriter` (`adapters.py`) и перенос старых форматов (`migration.py`). Остальное ядро обращается к сервису через имена,
выставленные в этом файле, и протоколы `interfaces/contracts.py`; внутренние файлы наружу не импортируются.

Имя папки `memory_service`, а не `memory`: `agent/memory/` это данные ядра (MEMORY_DIR) и пакетом быть не может.
"""
from . import episode_routes, episode_service, migration, scope  # noqa: E402,F401  (модули-пространства имён: маршруты сна и эпизодов, перенос формата, области графа)
from .adapters import UserMemoryAdapter, writer_for_companion  # noqa: E402,F401
from .capture import SPEAKER_TAG, MemoryCapture  # noqa: E402,F401
from .episode_service import EpisodeService  # noqa: E402,F401
from .graph import Fact, GraphMemory, SqliteBackend  # noqa: E402,F401
from .user import UserMemory  # noqa: E402,F401

__all__ = ["EpisodeService", "Fact", "GraphMemory", "MemoryCapture", "SPEAKER_TAG", "SqliteBackend", "UserMemory",
           "UserMemoryAdapter", "episode_routes", "episode_service", "migration", "scope", "writer_for_companion"]
