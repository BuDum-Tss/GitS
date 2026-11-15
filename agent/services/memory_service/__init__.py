"""Сервис памяти (ТЗ 04, 27, 29): всё, что персонаж помнит.

Единая память это эпизоды (`episode_store.py`, `episode_memory.py`, `episode_recall.py`): жизнь, канон из memories.yaml
(`character_seed.py`) и заметки «запомни»; перенос старого графа (`graph_import.py`). Диалог и активность (`user.py`), эпизоды,
сон и дневник (`episodes.py`, `episode_*.py`, `sleep*.py`, `diary.py`), явное «запомни» (`capture.py`), адаптеры контрактов `MemoryReader`
и `MemoryWriter` (`adapters.py`) и перенос старых форматов (`migration.py`). Остальное ядро обращается к сервису через имена,
выставленные в этом файле, и протоколы `interfaces/contracts.py`; внутренние файлы наружу не импортируются.

Имя папки `memory_service`, а не `memory`: `agent/memory/` это данные ядра (MEMORY_DIR) и пакетом быть не может.
"""
from . import episode_routes, episode_service, migration  # noqa: E402,F401  (модули-пространства имён: маршруты сна и эпизодов, перенос формата)
from .adapters import UserMemoryAdapter  # noqa: E402,F401
from .capture import SPEAKER_TAG, MemoryCapture  # noqa: E402,F401
from .episode_service import EpisodeService  # noqa: E402,F401
from .user import UserMemory  # noqa: E402,F401

__all__ = ["EpisodeService", "MemoryCapture", "SPEAKER_TAG", "UserMemory", "UserMemoryAdapter", "episode_routes", "episode_service",
           "migration"]
