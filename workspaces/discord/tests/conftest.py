"""Общие настройки тестов модуля Discord: пути, изолированная папка памяти, никаких секретов и сети.

discord.py тестам не нужен: если библиотеки в системном Python нет, подставляется минимальная подмена модуля `discord`
(Intents, Client, AllowedMentions), чтобы проверки «библиотека установлена» проходили. Настоящий клиент нигде не создаётся.
"""
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
GITS = HERE.parent.parent
for p in (GITS / "test", GITS / "agent" / "interfaces" / "workspace", GITS / "agent", HERE):       # как в main.py модуля: свои файлы первыми
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

try:
    import discord  # noqa: F401
except ImportError:
    fake = types.ModuleType("discord")

    class _Intents:
        @classmethod
        def none(cls):
            return cls()

    class _AllowedMentions:
        def __init__(self, **kw):
            self.kw = kw

    class _Client:
        def __init__(self, **kw):
            raise AssertionError("тесты не создают настоящий клиент Discord")

    fake.Intents, fake.AllowedMentions, fake.Client = _Intents, _AllowedMentions, _Client
    sys.modules["discord"] = fake

from services.conversation import approvals  # noqa: E402
from services.classifier import decide  # noqa: E402
from services.tracing import events_log  # noqa: E402
from host import settings  # noqa: E402  (только для сквозных тестов с настоящим ядром; сам модуль settings не импортирует)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    mem = tmp_path / "memory"
    mem.mkdir()
    monkeypatch.setenv("MEMORY_DIR", str(mem))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("GITS_SECRETS_FILE", str(tmp_path / "нет-такого-.env"))
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
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
