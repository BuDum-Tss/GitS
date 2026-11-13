"""Общий поиск в интернете: в реестре как инструмент ядра, отвечает по правилам."""
import asyncio
import tempfile

from host import settings
from services.runtime import web_search
from environments import builtin
from environments.registry import Registry
from services.memory_service.user import UserMemory


class _Dummy:
    def __getattr__(self, name):
        return None


def test_web_tool_is_registered_in_core_module(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    reg = Registry()
    builtin.register(reg, _Dummy(), UserMemory(d))
    env = reg.envs["web"]
    assert env.module == "core" and [t.id for t in env.tools] == ["web.search"]
    assert reg.get("web.search").risk == "auto"


def test_short_query_is_rejected():
    assert "короткий" in asyncio.run(web_search.search("ab"))
