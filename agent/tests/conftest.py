"""Общие настройки тестов ядра: каждый тест в своей папке памяти, без токена в настоящем %LOCALAPPDATA%."""
import pytest

import approvals
import settings


@pytest.fixture(autouse=True)
def _isolated_core(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))     # файл токена модулей (MOD-4)
    settings.init(str(mem))
    approvals.reset()
    yield
    approvals.reset()
