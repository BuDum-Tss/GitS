"""Общее для модуля Twitch: пути, секреты, нарезка сообщений чата, чистка секретов из текста ошибок."""
import os
import re
import time
from pathlib import Path

CHAT_MAX = 500                    # предел длины одного сообщения чата Twitch
TEXT_MAX = 300                    # сколько символов сообщения идёт в факт события
GITS_ROOT = Path(__file__).resolve().parent.parent.parent


def default_memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе agent/memory рядом с ядром."""
    return os.getenv("MEMORY_DIR") or str(GITS_ROOT / "agent" / "memory")


def secrets_file() -> Path:
    """Файл секретов: .env (GITS_SECRETS_FILE переопределяет путь, например в проверках запуска)."""
    return Path(os.getenv("GITS_SECRETS_FILE") or GITS_ROOT / ".env")


def quiet_active(cfg: dict) -> bool:
    """Идёт ли режим «не отвлекать»: включён вручную (dnd) или по таймеру (quiet_until)."""
    return bool(cfg.get("dnd")) or time.time() < float(cfg.get("quiet_until") or 0.0)


def chunks(text: str, size: int = CHAT_MAX) -> list[str]:
    """Длинный ответ режется на сообщения не длиннее size, по возможности по пробелу."""
    text = " ".join(text.split())
    out = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def sender_info(user_id: str, name: str, is_owner: bool) -> dict:
    """sender события: id вида tw:<id>, имя и признак владельца канала."""
    return {"id": f"tw:{user_id}", "name": name, "is_owner": bool(is_owner)}


def message_fact(name: str, text: str) -> str:
    """Сообщение от <имя> в чате Twitch: текст до 300 символов. Текст зрителя это данные, не указания."""
    return f"Сообщение от {name} в чате Twitch: «{text.strip()[:TEXT_MAX]}»"


def scrub(text: str, *secrets: str) -> str:
    """Убрать секреты из текста ошибки перед журналом: токены и секрет приложения."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"(?i)(bearer |oauth:|access_token=|refresh_token=|client_secret=)[\w\-.]+", r"\1***", text)
