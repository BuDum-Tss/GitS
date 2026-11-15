"""Запуск сервера распознавания: `python -m stt.main` (в контейнере CMD). Настройки из переменных окружения (stt/config.py)."""
import logging
import os

# кэш моделей только для чтения и без сети: сервис ничего не качает (см. README); временные файлы в /tmp
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from aiohttp import web  # noqa: E402

from .app import create_app  # noqa: E402
from .config import SttConfig  # noqa: E402
from .engine import build_engine  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = SttConfig.from_env()
    engine = build_engine(config)
    logging.getLogger("stt").info("Запуск: %s", config.public())
    web.run_app(create_app(config, engine), host=config.host, port=config.port, print=None)


if __name__ == "__main__":
    main()
