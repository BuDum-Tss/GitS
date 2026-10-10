"""Общий поиск в интернете: в реестре как инструмент ядра, без Claude отвечает по правилам."""
import asyncio
import tempfile

import claude_quota
import settings
import web_search
from environments import builtin
from environments.registry import Registry
from user_memory import UserMemory


class _Dummy:
    def __getattr__(self, name):
        return None


def test_web_tool_is_registered_in_core_module(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    reg = Registry()
    builtin.register(reg, _Dummy(), None, UserMemory(d))
    env = reg.envs["web"]
    assert env.module == "core" and [t.id for t in env.tools] == ["web.search"]
    assert reg.get("web.search").risk == "auto"


def test_short_query_and_quota(monkeypatch):
    assert "короткий" in asyncio.run(web_search.search("ab"))
    monkeypatch.setattr(claude_quota, "blocked", lambda: True)
    monkeypatch.setattr(claude_quota, "until_text", lambda: "19:40")
    assert "19:40" in asyncio.run(web_search.search("rimworld roof support"))
