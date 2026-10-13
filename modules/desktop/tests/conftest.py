"""Общие настройки тестов модуля desktop: пути, изолированная папка памяти, без экрана, мыши, OCR, LM Studio и Claude."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "sdk", GITS / "agent", HERE):                 # как в main.py модуля: свои файлы первыми
    if str(p) in sys.path:
        sys.path.remove(str(p))
    sys.path.insert(0, str(p))

import events_log  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-такого-secrets.env"))
    monkeypatch.delenv("GITS_DESKTOP_FAKE", raising=False)
    events_log.init(str(mem))
    yield
    events_log.flush()
