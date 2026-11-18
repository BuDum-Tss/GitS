"""Общие настройки тестов адаптера кабелей: пути как в main.py, без секретов, устройств и сети (движок поднимается в процессе)."""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "agent", GITS / "agent" / "interfaces" / "workspace", HERE):       # свои файлы первыми, как в main.py
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-такого-.env"))
    monkeypatch.delenv("GITS_TOKEN", raising=False)
    monkeypatch.delenv("GITS_VOICE_URL", raising=False)
