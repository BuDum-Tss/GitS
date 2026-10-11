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
    """Помощник Claude живёт в модуле desktop (ТЗ 14): его fallback-инструмент попадает к планировщику при любом отборе."""
    from environments.base import Environment, Tool
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    reg = Registry()
    builtin.register(reg, _Dummy(), UserMemory(d))
    helper = Tool(id="desktop.claude_task", env="desktop", name="claude_task", description="Сложные задачи на компьютере.",
                  always=True, keywords=("файл", "код"))
    reg.add(Environment(id="desktop", title="Компьютер", description="Экран и Claude", tools=[helper], module="desktop"))
    picked = reg.search("установи и настрой telethon telegram", k=3)
    assert "desktop.claude_task" in [t.id for t in picked]
    assert reg.get("system.ask_claude") is helper                 # прежний id ведёт на новый инструмент (алиас)
