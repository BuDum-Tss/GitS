"""Точка входа модуля Telegram (ТЗ 10). Запускает супервизор ядра: `python main.py` из папки модуля.

Секреты (TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL, TELEGRAM_API_ID, TELEGRAM_API_HASH) модуль читает сам из agent/secrets.env
(путь можно заменить переменной GITS_SECRETS_FILE); их значения нигде не печатаются. Без секретов модуль работает и честно
сообщает в состоянии «не настроен».
"""
import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета gits-sdk; свои файлы модуля первыми. Внутренностей ядра (agent/) модуль не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "sdk")]

from dotenv import load_dotenv  # noqa: E402

from common import default_memory_dir, secrets_file  # noqa: E402

load_dotenv(secrets_file())                       # секреты не в git; файла нет - модуль «не настроен»

from telegram_module import TelegramModule  # noqa: E402
from gits_sdk import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402

log = logging.getLogger("telegram")


async def amain() -> int:
    memory_dir = default_memory_dir()
    client = ModuleClient(build_manifest())
    module = TelegramModule(client, memory_dir)
    module.start()
    try:
        return await client.run()
    finally:
        await module.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    # httpx пишет в журнал полный адрес запроса, а в нём токен бота: журнал не должен его хранить
    for noisy in ("httpx", "httpcore", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
