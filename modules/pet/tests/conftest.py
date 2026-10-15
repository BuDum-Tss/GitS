"""Общие настройки тестов модуля питомца: пути, никакого окна и сети (окно подменяется записью)."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "sdk", GITS / "agent", HERE):                 # как в main.py модуля: свои файлы первыми
    if str(p) in sys.path:
        sys.path.remove(str(p))
    sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("GITS_PET_HEADLESS", raising=False)
    monkeypatch.delenv("GITS_DASHBOARD_URL", raising=False)
    yield
