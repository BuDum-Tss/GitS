"""Реестр окружений: отбор инструментов для планировщика всегда оставляет Claude-помощника."""
import tempfile

from host import settings
from environments import builtin
from environments.registry import Registry
from services.memory_service.user import UserMemory


class _Dummy:
    def __getattr__(self, name):
        return None


def test_search_always_includes_code_helper(monkeypatch):
    """Помощник Claude живёт в модуле desktop (ТЗ 14): его fallback-инструмент попадает к планировщику при любом отборе."""
    from environments.base import Environment, Tool
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    reg = Registry()
    builtin.register(reg, _Dummy(), UserMemory(d))
    helper = Tool(id="desktop.code_task", env="desktop", name="code_task", description="Сложные задачи на компьютере.",
                  always=True, keywords=("файл", "код"))
    reg.add(Environment(id="desktop", title="Компьютер", description="Экран и код", tools=[helper], module="desktop"))
    picked = reg.search("установи и настрой telethon telegram", k=3)
    assert "desktop.code_task" in [t.id for t in picked]
    assert reg.get("system.ask_claude") is helper                 # прежний id ведёт на новый инструмент (алиас)
