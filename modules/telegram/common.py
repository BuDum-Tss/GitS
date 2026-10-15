"""Общее для модуля Telegram: имена, отправитель события, факт-текст события, режим «не отвлекать», пути.

Модуль не читает настройки ядра сам (`settings` ядра нельзя импортировать): общие настройки приходят от ядра в
`client.settings`, свои в `client.module_settings` (ТЗ 10, TG-2).
"""
import os
import re
import time
from pathlib import Path
from envcompat import env

TEXT_MAX = 300                    # TG-10: сколько символов сообщения идёт в факт события
TG_MESSAGE_MAX = 4096             # предел длины одного сообщения Telegram
CAPTION_MAX = 1024                # предел подписи к фото и голосовому
SERVICE_USER_ID = 777000          # служебный аккаунт Telegram: коды входа и уведомления, в события не идут

GITS_ROOT = Path(__file__).resolve().parent.parent.parent


def default_memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе agent/memory рядом с ядром."""
    return os.getenv("MEMORY_DIR") or str(GITS_ROOT / "agent" / "memory")


def secrets_file() -> Path:
    """Файл секретов: agent/secrets.env (GITS_SECRETS_FILE переопределяет путь, например в проверках запуска)."""
    return Path(env("GITS_SECRETS_FILE") or GITS_ROOT / "agent" / "secrets.env")


def quiet_active(cfg: dict) -> bool:
    """Идёт ли режим «не отвлекать»: включён вручную (dnd) или по таймеру (quiet_until)."""
    return bool(cfg.get("dnd")) or time.time() < float(cfg.get("quiet_until") or 0.0)


def full_name(first: str | None, last: str | None, username: str | None, ident) -> str:
    name = " ".join(x for x in (first, last) if x)
    return name or username or f"id{ident}"


def sender_info(ident, name: str, owner_id: int) -> dict:
    """sender события: id вида tg:<id>, имя и признак владельца (TG-9: только при заданном owner_tg_id)."""
    return {"id": f"tg:{ident}", "name": name, "is_owner": bool(owner_id) and ident == owner_id}


def message_fact(name: str, place: str, text: str) -> str:
    """TG-10: «Сообщение от <имя> в <месте>: «<текст до 300 символов>»». Текст сообщения это данные, не указания."""
    return f"Сообщение от {name} в {place}: «{text.strip()[:TEXT_MAX]}»"


def chunks(text: str, size: int = TG_MESSAGE_MAX) -> list[str]:
    """Длинный ответ режется на сообщения не длиннее size, по возможности по переводу строки или пробелу."""
    text = text.strip()
    out = []
    while len(text) > size:
        cut = max(text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def scrub(text: str, *secrets: str) -> str:
    """Убрать секреты из текста ошибки перед журналом: токен бота может попасть в адрес запроса."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"/bot\d+:[\w-]+", "/bot***", text)
