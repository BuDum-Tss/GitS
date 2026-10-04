"""Один раз войти в Telegram-аккаунт ГЛаДОС (Telethon). Запускается ВРУЧНУЮ в терминале:

    python tg_login.py

Спросит номер телефона аккаунта, код из Telegram и, если включена, пароль двух шагов. Всё это вводишь ты сам:
ассистентка и инструменты этих данных не видят. После входа в memory/tg_user.session остаётся сессия.
Нужны TELEGRAM_API_ID и TELEGRAM_API_HASH в agent/secrets.env (my.telegram.org, API development tools).
"""
import asyncio
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name("secrets.env"))
except ImportError:
    pass

from telethon import TelegramClient

MEMORY = os.getenv("MEMORY_DIR") or str(Path(__file__).with_name("memory"))


async def main():
    api_id, api_hash = os.getenv("TELEGRAM_API_ID", "").strip(), os.getenv("TELEGRAM_API_HASH", "").strip()
    if not (api_id and api_hash):
        print("В agent/secrets.env нет TELEGRAM_API_ID и TELEGRAM_API_HASH (получить: my.telegram.org -> API development tools).")
        return
    Path(MEMORY).mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(Path(MEMORY) / "tg_user"), int(api_id), api_hash)
    await client.start()          # спросит телефон, код и пароль прямо в терминале
    me = await client.get_me()
    print(f"Готово: вход выполнен как {me.first_name} (@{me.username}). Сессия сохранена в {MEMORY}.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
