"""Точка входа модуля OBS. Запускает супервизор ядра: `python main.py` из папки модуля.

Адрес и пароль obs-websocket (OBS_WEBSOCKET_URL, по умолчанию ws://127.0.0.1:4455, и OBS_WEBSOCKET_PASSWORD) модуль читает сам из
.env (путь можно заменить переменной GITS_SECRETS_FILE); значения нигде не печатаются. В OBS: Инструменты, Настройки
WebSocket-сервера, включить сервер и задать пароль. Без OBS модуль работает и честно сообщает в состоянии «не на связи»."""
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITS = HERE.parent.parent
# SDK до установки пакета gits-sdk; свои файлы модуля первыми. Внутренностей ядра (agent/) модуль не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(GITS / "sdk")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(os.getenv("GITS_SECRETS_FILE") or GITS / ".env"))

from gits_sdk import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402
from obs_module import ObsModule  # noqa: E402


async def amain() -> int:
    client = ModuleClient(build_manifest())
    module = ObsModule(client)
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
