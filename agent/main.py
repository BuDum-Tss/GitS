"""Точка входа."""
import logging
import os

import nowindow  # noqa: F401  (побочный эффект: скрывает окна консоли)
from dotenv import load_dotenv

# Загружаем .env до импорта ядра (на случай если задан ANTHROPIC_API_KEY)
load_dotenv()
load_dotenv("secrets.env")   # токены (не в git): TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL

# Агент RimWorld живёт в отдельном репозитории (rimworld-agent); здесь только путь к его MCP-серверу для окружения «RimWorld»
os.environ.setdefault("RIMWORLD_AGENT_DIR", r"C:\MyProjects\rimworld-agent")

import core_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    # Agent SDK использует либо ANTHROPIC_API_KEY либо OAuth из ~/.claude/.credentials.json
    config = {
        "rimworld_api": os.getenv("RIMWORLD_API", "http://localhost:8765"),
        "memory_dir":   os.getenv("MEMORY_DIR", "./memory"),
    }

    core_service.run(config)


if __name__ == "__main__":
    main()
