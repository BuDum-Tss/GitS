"""Общие настройки тестов ядра: каждый тест в своей папке памяти, без токена в настоящем %LOCALAPPDATA%."""
import pytest

from services.conversation import approvals
from services.character import character
from services.classifier import decide
from services.tracing import events_log
from services.character import prompts
from services.conversation import resident
from host import settings
from interfaces.contracts import EndEvent


@pytest.fixture(autouse=True)
def _isolated_core(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))     # файл токена модулей (MOD-4)
    # страховка: если тест всё же запустит настоящий модуль Telegram, настоящие секреты он не прочитает
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-.env"))
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

    class NullRuntime:                                 # единый агент без модели: сессия принимает запрос и ничего не отвечает
        can_steer = True

        async def start(self, system, tools):
            pass

        async def run(self, prompt):
            yield EndEvent("ok")

        async def steer(self, text):
            return False

        async def abort(self):
            pass

        async def reset(self, forget=True):
            pass

        async def close(self):
            pass
    monkeypatch.setattr(resident, "_default_runtime", NullRuntime)
    yield
    events_log.flush()                                  # фоновая запись журнала не должна уйти в папку следующего теста
    approvals.reset()
