"""Общие настройки тестов модуля OBS: пути, никаких секретов и сети."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "sdk", HERE):                      # как в main.py модуля: свои файлы первыми
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-такого-.env"))
    for name in ("OBS_WEBSOCKET_URL", "OBS_WEBSOCKET_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
