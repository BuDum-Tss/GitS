"""ТЗ 33: рабочее место в ядре. Видимость инструментов по узлу, панель узла в контексте агента, срочность по фокусу и узлу."""
import asyncio
from types import SimpleNamespace

from app.attention_core import Item
from app.node_panel import NodePanel
from app.normalizer import Normalizer
from environments.base import Environment, Tool
from environments.registry import Registry

MANIFEST = {"id": "game", "nodes": [
    {"path": "build", "title": "Строительство", "context": [
        {"tool": "resources", "mode": "on_change", "max_chars": 80}, {"tool": "queue", "mode": "on_enter"}],
     "watch": {"resources": 1, "social": -1}},
    {"path": "work", "title": "Работа"}]}


def _item(urgency):
    return Item(id="a-1", module="game", kind="game.low", text="x", data={}, sender=None, reply_channel=None,
                addressed_to_me=None, urgency=urgency, needs_reply=False, memorable=False, created=0.0, expires=None,
                dedupe_key=None)


# ── видимость инструментов ────────────────────────────────────────────────────

def _registry(node):
    reg = Registry()
    tools = [Tool(id="game.open", env="game", name="open", description="d"),
             Tool(id="game.place", env="game", name="place", description="d", node="build"),
             Tool(id="game.set_hours", env="game", name="set_hours", description="d", node="schedule")]
    reg.add(Environment(id="game", title="g", description="d", tools=tools, module="game"))
    reg.node_of = lambda module: node
    return reg


def test_tools_of_other_nodes_are_hidden():
    names = {t.name for t in _registry("build").active_tools()}
    assert names == {"open", "place"}                                  # set_hours принадлежит узлу schedule


def test_without_reported_node_everything_is_visible():
    assert {t.name for t in _registry(None).active_tools()} == {"open", "place", "set_hours"}


# ── панель узла ───────────────────────────────────────────────────────────────

class Game:
    def __init__(self):
        self.answers = {"resources": "дерево 120, сталь 4", "queue": "чертежей 2"}
        self.calls = []

    async def run(self, tool_id, args):
        self.calls.append(tool_id)
        return True, self.answers[tool_id.split(".")[1]], None


def _panel(game, env="game", node="build", now=None):
    clock = (lambda: now[0]) if now else (lambda: 0.0)
    return NodePanel(env_of=lambda: env, manifest_of=lambda m: MANIFEST if m == "game" else None, node_of=lambda m: node,
                     run_tool=game.run, clock=clock)


def test_panel_shows_everything_on_enter_and_marks_unchanged_afterwards():
    game, now = Game(), [0.0]
    panel = _panel(game, now=now)
    asyncio.run(panel.refresh())
    text = panel.block()
    assert "Рабочее место: game/build (Строительство)" in text
    assert "resources: дерево 120, сталь 4" in text and "queue: чертежей 2" in text
    now[0] = 10.0                                                      # прошло больше TTL, узел тот же
    asyncio.run(panel.refresh())
    again = panel.block()
    assert "resources: без изменений" in again and "queue" not in again           # on_enter показывается только при входе
    game.answers["resources"] = "дерево 90, сталь 4"
    now[0] = 20.0
    asyncio.run(panel.refresh())
    assert "resources: дерево 90, сталь 4" in panel.block()


def test_panel_is_empty_outside_the_module_or_node():
    game = Game()
    for env, node in (("assistant", "build"), ("game", None), ("game", "work")):
        panel = _panel(game, env=env, node=node)
        asyncio.run(panel.refresh())
        assert panel.block() == ""
    assert game.calls == []                                            # запросов к модулю не было


def test_panel_survives_a_failing_query_and_respects_budget():
    game = Game()

    async def run(tool_id, args):
        if tool_id.endswith("queue"):
            raise RuntimeError("модуль не ответил")
        return True, "x" * 500, None
    panel = _panel(game)
    panel._run = run
    asyncio.run(panel.refresh())
    text = panel.block()
    assert "resources:" in text and "queue" not in text and len(text) < 200      # max_chars 80 у запроса


# ── срочность ────────────────────────────────────────────────────────────────

INFO = {"kind": "game.low", "default_urgency": 2, "urgency_unfocused": 1, "topics": ["resources"]}


def test_unfocused_event_at_default_level_takes_unfocused_urgency():
    item = _item(2)
    Normalizer.shift_urgency(item, INFO, False, None)
    assert item.urgency == 1
    escalated = _item(4)                                               # модуль сам поднял срочность: остаётся
    Normalizer.shift_urgency(escalated, INFO, False, None)
    assert escalated.urgency == 4


def test_focused_event_is_shifted_by_the_node_topic():
    item = _item(2)
    Normalizer.shift_urgency(item, INFO, True, {"resources": 1})
    assert item.urgency == 3
    other = _item(2)
    Normalizer.shift_urgency(other, INFO, True, {"social": -1})        # тема не совпала
    assert other.urgency == 2


def test_critical_event_is_never_lowered_below_four_by_a_node():
    info = {"kind": "game.raid", "default_urgency": 5, "topics": ["resources"]}
    item = _item(5)
    Normalizer.shift_urgency(item, info, True, {"resources": -2})
    assert item.urgency == 4


def test_nothing_changes_without_declared_fields():
    item = _item(3)
    Normalizer.shift_urgency(item, {"kind": "game.low", "default_urgency": 3}, True, {"resources": 2})
    assert item.urgency == 3
    Normalizer.shift_urgency(item, None, True, {"resources": 2})
    assert item.urgency == 3
    assert SimpleNamespace  # импорт нужен только для типов тестов выше


# ── узел из state и поле tools[].node в окружении модуля ──────────────────────────

def test_module_registry_keeps_reported_node_and_passes_tool_node_to_the_environment():
    import module_registry
    from gits_sdk import protocol as P
    reg = module_registry.ModuleRegistry(Registry(), settings_source=lambda: {})
    manifest = {"id": "game", "title": "Игра", "description": "d", "version": "1.0.0",
                "tools": [{"name": "place", "description": "d", "params": {}, "risk": "auto", "availability": "always",
                           "node": "build"}],
                "nodes": [{"path": "build", "title": "Строительство"}]}
    rec = reg.declare("game")
    reg._build_environment(rec, P.Manifest.from_dict(manifest))
    assert reg.envs.get("game.place").node == "build"
    rec.connected = True
    reg.set_state("game", "в игре", {}, "build")
    assert reg.node("game") == "build"
    reg.set_state("game", "в игре", {})                  # старый модуль без node: узел сбрасывается
    assert reg.node("game") is None
