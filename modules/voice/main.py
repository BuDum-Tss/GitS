"""Точка входа модуля голоса (ТЗ 12). Запускает супервизор ядра: `python main.py` из папки модуля.

Секретов у модуля нет (Fish Speech и WhisperX локальные), но файл agent/secrets.env читается, как у других модулей монорепо:
в нём могут лежать адреса и пути (FISH_URL, WHISPERX_URL, GITS_STT_MODEL). GITS_VOICE_FAKE=1 подменяет синтез и
воспроизведение тишиной и не запускает микрофон и слух: так проверяют запуск без звука.
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

load_dotenv(secrets_file())                       # адреса сервисов и модели; файла нет - значения по умолчанию

from app import VoiceModule  # noqa: E402
from gits_sdk import ModuleClient  # noqa: E402
from manifest import build_manifest  # noqa: E402


async def amain() -> int:
    memory_dir = default_memory_dir()
    client = ModuleClient(build_manifest())
    module = VoiceModule(client, memory_dir)
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
