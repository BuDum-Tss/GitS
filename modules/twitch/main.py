"""Точка входа модуля Twitch. Запускает супервизор ядра: `python main.py` из папки модуля.

Секреты (TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL) модуль читает сам из agent/secrets.env (путь можно заменить
переменной GITS_SECRETS_FILE); значения нигде не печатаются. Токены входа лежат в папке памяти (twitch_tokens.json), их создаёт
`tw_login.py`. Без настроек модуль работает и честно сообщает в состоянии «не настроен».
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

from gits_sdk import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402
from twitch_module import TwitchModule  # noqa: E402


async def amain() -> int:
    client = ModuleClient(build_manifest())
    module = TwitchModule(client, default_memory_dir())
    module.start()
    try:
        return await client.run()
    finally:
        await module.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
