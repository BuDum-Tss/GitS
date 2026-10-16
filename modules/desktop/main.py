"""Точка входа модуля desktop (ТЗ 14). Запускает супервизор ядра: `python main.py` из папки модуля.

Секретов у модуля нет, но agent/secrets.env читается, как у других модулей монорепо. GITS_DESKTOP_FAKE=1 отключает опрос
экрана и ввод, а инструменты, которые трогают экран, программы и Claude, отвечают `unavailable` (проверка запуска).
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GITS = HERE.parent.parent
# SDK до установки пакета gits-sdk; свои файлы модуля первыми. Внутренностей ядра (agent/) модуль не импортирует (ТЗ 31)
sys.path[:0] = [str(HERE), str(GITS / "sdk")]

from envcompat import env  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(env("GITS_SECRETS_FILE") or GITS / "agent" / "secrets.env"))

from app import DesktopModule  # noqa: E402
from gits_sdk import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402


def memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе agent/memory рядом с ядром."""
    return os.getenv("MEMORY_DIR") or str(GITS / "agent" / "memory")


async def amain() -> int:
    client = ModuleClient(build_manifest())
    module = DesktopModule(client)
    module.start()
    try:
        return await client.run()
    finally:
        await module.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)       # httpx пишет в журнал полный адрес запроса
    try:
        code = asyncio.run(amain())
    except KeyboardInterrupt:
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
