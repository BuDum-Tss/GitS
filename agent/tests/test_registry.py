"""Реестр окружений: отбор инструментов для планировщика всегда оставляет Claude-помощника."""
import tempfile

import settings
from environments import builtin
from environments.registry import Registry
from user_memory import UserMemory


class _Dummy:
    def __getattr__(self, name):
        return None


def test_search_always_includes_claude_helper(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    reg = Registry()
    builtin.register(reg, _Dummy(), None, None, UserMemory(d))
    picked = reg.search("установи и настрой telethon telegram", k=3)
    assert "system.ask_claude" in [t.id for t in picked]
