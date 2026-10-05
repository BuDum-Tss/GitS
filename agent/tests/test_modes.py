"""Профили и батарейка: выбор профиля, сон без заряда, отключение окружений вне профиля, граф и заряд."""
import asyncio
import tempfile
import time

import claude_battery
import claude_quota
import modes
import settings
from environments.base import Environment, Tool
from environments.registry import Registry


def _manager(running=False, sleeping=False):
    return modes.ModeManager(game_running=lambda: running, battery=lambda: {"sleeping": sleeping, "level": 0.5})


def test_auto_profile():
    assert _manager().current() == "assistant"
    assert _manager(running=True).current() == "rimworld"
    assert _manager(running=True, sleeping=True).current() == "sleep"       # без заряда сон важнее игры


def test_override_planned_and_sleep_wins():
    m = _manager()
    assert m.set_override("rimworld") is None and m.current() == "rimworld"
    assert m.set_override("minecraft")                                         # профиль ещё не написан
    assert m.set_override("nonsense")
    m2 = _manager(sleeping=True)
    m2.override = "rimworld"
    assert m2.current() == "sleep"
    assert m.set_override("auto") is None and m.current() == "assistant"


def test_registry_gates_environments_by_profile():
    reg = Registry()
    tool = lambda env: Tool(id=f"{env}.t", env=env, name="t", description="d")   # noqa: E731
    reg.add(Environment(id="system", title="ПК", description="", tools=[tool("system")]))
    reg.add(Environment(id="rimworld", title="RW", description="", tools=[tool("rimworld")], module="rimworld"))
    m = _manager()
    reg.gate = m.module_active
    assert [t.id for t in reg.active_tools()] == ["system.t"]
    m.game_running = lambda: True
    assert {t.id for t in reg.active_tools()} == {"system.t", "rimworld.t"}
    m.battery = lambda: {"sleeping": True}
    assert [t.id for t in reg.active_tools()] == ["system.t"]


async def _none():
    return None


def test_graph_hub_profiles_and_subgraph(monkeypatch):
    async def sub(profile):
        return {"nodes": [{"id": "agent", "label": "A", "root": True}], "edges": []}
    monkeypatch.setattr(modes, "_fetch_subgraph", sub)
    g = asyncio.run(_manager(running=True).graph())
    ids = {n["id"] for n in g["nodes"]}
    assert {"assistant", "rimworld", "minecraft", "programming", "editing", "sleep"} <= ids
    assert [n["id"] for n in g["nodes"] if n["current"]] == ["rimworld"]
    assert all(e["from"] == "assistant" for e in g["edges"])
    assert g["subgraphs"]["rimworld"]["nodes"][0]["id"] == "agent"
    assert g["subgraphs"]["rimworld"]["live"] is True
    monkeypatch.setattr(modes, "_fetch_subgraph", lambda p: _none())          # агент не отвечает: подграф всё равно доступен
    off = asyncio.run(_manager().graph())["subgraphs"]["rimworld"]
    assert off["live"] is False and any(n["id"] == "observe" for n in off["nodes"])
    assert all(n["drill"] for n in g["nodes"])
    assert [n["modules"] for n in g["nodes"] if n["id"] == "rimworld"] == [["rimworld"]]


def test_battery_level_and_sleep(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    settings.update(claude_token_budget=1_000_000)
    assert claude_battery.status()["level"] == 1.0
    claude_battery.record(250_000, "t")
    st = claude_battery.status()
    assert st["used"] == 250_000 and st["level"] == 0.75 and not st["sleeping"]
    claude_battery.record(800_000, "t")
    assert claude_battery.status()["sleeping"]
    assert claude_battery.status(now=time.time() + claude_battery.WINDOW_S + 5)["level"] == 1.0   # окно освободилось


def test_battery_tokens_skip_cache_reads_and_limit_message_empties_it(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    assert claude_battery.tokens_of({"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 10**6}) == 12
    assert claude_quota.note("You've hit your session limit · resets 7:40pm")
    st = claude_battery.status()
    assert st["level"] == 0.0 and st["sleeping"] and st["blocked"]


def test_battery_recovery_times(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    settings.update(claude_token_budget=1_000_000)
    assert claude_battery.status()["full_at"] is None
    t0 = time.time()
    claude_battery.record(600_000, "a")
    claude_battery.record(500_000, "b")
    st = claude_battery.status()
    assert st["sleeping"] and st["next_release_tokens"] == 600_000
    assert abs(st["wake_at"] - (t0 + claude_battery.WINDOW_S)) < 5       # после выхода первой порции спать уже не нужно
    assert st["full_at"] >= st["next_release_at"]
