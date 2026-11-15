"""Точка входа модуля Discord. Запускает супервизор ядра: `python main.py` из папки модуля.

Секрет (DISCORD_BOT_TOKEN) модуль читает сам из .env (путь можно заменить переменной GITS_SECRETS_FILE); значение нигде не
печатается. Без токена модуль работает и честно сообщает в состоянии «не настроен».
"""
import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета gits-sdk; свои файлы модуля первыми. Внутренностей ядра (agent/) модуль не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "agent" / "interfaces" / "workspace")]

from dotenv import load_dotenv  # noqa: E402

from common import configure_logging, default_memory_dir, secrets_file  # noqa: E402

load_dotenv(secrets_file())                       # секреты не в git; файла нет - модуль «не настроен»

from discord_module import DiscordModule  # noqa: E402
from gits_sdk import WorkspaceClient  # noqa: E402
from manifest import build_manifest  # noqa: E402

log = logging.getLogger("discord")


async def amain() -> int:
    memory_dir = default_memory_dir()
    avatar_dir = Path(memory_dir).resolve() / "discord_avatars"      # аватарки серверов и собеседников для панели (поле avatar карточек)
    avatar_dir.mkdir(parents=True, exist_ok=True)
    client = WorkspaceClient(build_manifest([str(avatar_dir)]))
    module = DiscordModule(client, memory_dir)
    module.start()
    try:
        return await client.run()
    finally:
        await module.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    configure_logging()                  # токен бота не должен попасть в журнал: библиотеки тише, на обработчики очистка секретов
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
