"""Общее для модуля голоса: пути, временная папка, разбор настроек (ТЗ 12).

Модуль не читает настройки ядра сам (`settings` ядра нельзя импортировать): общие настройки приходят от ядра в
`client.settings`, свои в `client.module_settings`.
"""
import os
import tempfile
from pathlib import Path

GITS_ROOT = Path(__file__).resolve().parent.parent.parent
TEMP_DIR_NAME = "glados_voice"          # VOICE-5: файлы синтеза лежат в %TEMP%\glados_voice\
TEMP_KEEP_S = 3600                      # VOICE-5: файлы старше часа удаляются при старте и раз в час
ECHO_TAIL_DEFAULT_MS = 600              # сколько микрофон закрыт после её речи (прежний ECHO_TAIL = 0,6 с)
FAKE_ENV = "GITS_VOICE_FAKE"            # 1: синтез, воспроизведение, микрофон и слух подменены (проверка запуска, без звука)


def default_memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе agent/memory рядом с ядром."""
    return os.getenv("MEMORY_DIR") or str(GITS_ROOT / "agent" / "memory")


def secrets_file() -> Path:
    """Файл секретов: agent/secrets.env (GLADOS_SECRETS_FILE переопределяет путь, например в проверках запуска)."""
    return Path(os.getenv("GLADOS_SECRETS_FILE") or GITS_ROOT / "agent" / "secrets.env")


def temp_dir() -> Path:
    """Временная папка модуля (GLADOS_VOICE_TMP заменяет путь: тесты)."""
    return Path(os.getenv("GLADOS_VOICE_TMP") or Path(tempfile.gettempdir()) / TEMP_DIR_NAME)


def fake_mode() -> bool:
    return os.getenv(FAKE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def sweep_dir(directory: Path, keep_s: float = TEMP_KEEP_S, now: float | None = None) -> int:
    """Удалить файлы старше keep_s секунд (VOICE-5). Возвращает, сколько удалено. Блокирующий: звать через to_thread."""
    import time
    cutoff = (time.time() if now is None else now) - keep_s
    removed = 0
    try:
        files = list(directory.iterdir())
    except OSError:
        return 0
    for f in files:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed
