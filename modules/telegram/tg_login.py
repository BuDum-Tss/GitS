"""Один раз войти в Telegram-аккаунт ГЛаДОС (Telethon). Запускается ВРУЧНУЮ в терминале:

    python modules/telegram/tg_login.py

Спросит номер телефона аккаунта, код из Telegram и, если включена, пароль двух шагов. Всё это вводишь ты сам:
ассистентка и инструменты этих данных не видят. После входа в папке памяти (MEMORY_DIR, по умолчанию agent/memory)
остаётся сессия tg_user.session.
Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в agent/secrets.env (my.telegram.org, API development tools).
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import default_memory_dir, secrets_file  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(secrets_file())
except ImportError:
    pass

from telethon import TelegramClient  # noqa: E402


async def main():
    memory = default_memory_dir()
    api_id, api_hash = os.getenv("TELEGRAM_API_ID", "").strip(), os.getenv("TELEGRAM_API_HASH", "").strip()
    if not (api_id and api_hash):
        print("В agent/secrets.env нет TELEGRAM_API_ID и TELEGRAM_API_HASH (получить: my.telegram.org -> API development tools).")
        return
    Path(memory).mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(Path(memory) / "tg_user"), int(api_id), api_hash)
    await client.start()          # спросит телефон, код и пароль прямо в терминале
    me = await client.get_me()
    print(f"Готово: вход выполнен как {me.first_name} (@{me.username}). Сессия сохранена в {memory}.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
