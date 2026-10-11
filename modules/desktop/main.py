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
# библиотечные файлы ядра без состояния (fast_brain, events_log, safety, claude_cli) и SDK до установки пакета glados-module;
# свои файлы модуля первыми
sys.path[:0] = [str(HERE), str(GITS / "agent"), str(GITS / "sdk")]

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(os.getenv("GLADOS_SECRETS_FILE") or GITS / "agent" / "secrets.env"))

import events_log  # noqa: E402
from app import DesktopModule  # noqa: E402
from glados_module import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402


def memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе agent/memory рядом с ядром."""
    return os.getenv("MEMORY_DIR") or str(GITS / "agent" / "memory")


async def amain() -> int:
    events_log.init(memory_dir())
    client = ModuleClient(build_manifest())
    module = DesktopModule(client)
    module.start()
    try:
        return await client.run()
    finally:
        await module.stop()
        await asyncio.to_thread(events_log.flush)


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
