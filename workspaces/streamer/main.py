"""Точка входа окружения «Стример» (Twitch и OBS в одном процессе). Запускает супервизор ядра: `python main.py` из папки окружения.

Секреты (TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL; OBS_WEBSOCKET_URL, по умолчанию ws://127.0.0.1:4455, и
OBS_WEBSOCKET_PASSWORD) окружение читает само из .env (путь можно заменить переменной GITS_SECRETS_FILE); значения нигде не
печатаются. Токены входа Twitch лежат в папке памяти (twitch_tokens.json), их создаёт `twitch/tw_login.py`. В OBS: Инструменты,
Настройки WebSocket-сервера, включить сервер и задать пароль. Без настроек Twitch и без OBS окружение работает и честно сообщает
в состоянии «не настроен» и «не на связи».
"""
import asyncio
import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# SDK до установки пакета gits-sdk; свои файлы окружения первыми. Внутренностей ядра (agent/) окружение не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "sdk")]

from dotenv import load_dotenv  # noqa: E402

from twitch.common import default_memory_dir, secrets_file  # noqa: E402

load_dotenv(secrets_file())                       # секреты не в git; файла нет - окружение «не настроено»

from gits_sdk import WorkspaceClient  # noqa: E402
from manifest import build_manifest  # noqa: E402
from streamer_module import StreamerModule  # noqa: E402


async def amain() -> int:
    client = WorkspaceClient(build_manifest())
    module = StreamerModule(client, default_memory_dir())
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
