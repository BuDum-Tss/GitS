"""Общие настройки тестов ядра: каждый тест в своей папке памяти, без токена в настоящем %LOCALAPPDATA%."""
import pytest

import approvals
import character
import decide
import events_log
import prompts
from app import resident
import settings


@pytest.fixture(autouse=True)
def _isolated_core(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))     # файл токена модулей (MOD-4)
    # страховка: если тест всё же запустит настоящий модуль Telegram, настоящие секреты он не прочитает
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-secrets.env"))
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL", "TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(name, raising=False)
    settings.init(str(mem))
    character._active = None            # персонаж процесса (ТЗ 05): каждый тест начинает с glados
    prompts.reload()
    approvals.reset()
    decide.forget_down()

    async def no_classifier(*args, **kwargs):          # тесты без сети: классификатор (сервис decider) недоступен
        return None
    monkeypatch.setattr(decide, "decide", no_classifier)

    class NullClient:                                  # единый агент без Claude: сессия принимает запрос и ничего не отвечает
        def __init__(self, options=None):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def interrupt(self):
            pass

        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield
    monkeypatch.setattr(resident, "ClaudeSDKClient", NullClient)
    yield
    events_log.flush()                                  # фоновая запись журнала не должна уйти в папку следующего теста
    approvals.reset()
