"""Общие настройки тестов модуля Telegram: пути, изолированная папка памяти, никаких секретов и сети."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "agent" / "tests", GITS / "sdk", GITS / "agent", HERE):       # как в main.py модуля: свои файлы первыми
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import approvals  # noqa: E402
import decide  # noqa: E402
import events_log  # noqa: E402
import settings  # noqa: E402  (только для сквозных тестов с настоящим ядром; сам модуль settings не импортирует)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("GLADOS_SECRETS_FILE", str(tmp_path / "нет-такого-secrets.env"))
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL", "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_EVERY_MINS"):
        monkeypatch.delenv(name, raising=False)
    events_log.init(str(mem))
    settings.init(str(mem))
    approvals.reset()
    decide.forget_down()

    async def no_classifier(*args, **kwargs):             # сквозные тесты без сети: классификатор (сервис decider) недоступен
        return None
    monkeypatch.setattr(decide, "decide", no_classifier)
    yield
    events_log.flush()
    approvals.reset()
