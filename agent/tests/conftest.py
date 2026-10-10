"""Общие настройки тестов ядра: каждый тест в своей папке памяти, без токена в настоящем %LOCALAPPDATA%."""
import pytest

import approvals
import decide
import events_log
import settings


@pytest.fixture(autouse=True)
def _isolated_core(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))     # файл токена модулей (MOD-4)
    settings.init(str(mem))
    approvals.reset()
    decide.forget_down()

    async def no_classifier(*args, **kwargs):          # тесты без сети: классификатор (сервис decider) недоступен
        return None
    monkeypatch.setattr(decide, "decide", no_classifier)
    yield
    events_log.flush()                                  # фоновая запись журнала не должна уйти в папку следующего теста
    approvals.reset()
