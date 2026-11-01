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


# ── рабочий цикл автономной среды ─────────────────────────────────────────────

class _Agent:
    def __init__(self):
        self.busy, self.last_end = False, 0.0


def _loop(env, manifest, agent, now, sent, *, settings=None, quiet=False, blocked=False):
    from app.work_loop import WorkLoop

    async def emit(body):
        sent.append(body)
    return WorkLoop(env_of=lambda: env[0], manifest_of=lambda m: manifest if m == "game" else None, agent=agent, emit=emit,
                    settings=lambda: settings or {}, quiet=lambda: quiet, blocked=lambda: blocked, clock=lambda: now[0])


WS_MANIFEST = {"workspace": {"id": "game", "title": "Игра", "purpose": "играть вместе", "autonomous": True, "work_interval_s": 100}}


def test_work_loop_ticks_after_the_interval_and_only_when_the_agent_is_free():
    env, now, sent, agent = ["game"], [1000.0], [], _Agent()
    loop = _loop(env, WS_MANIFEST, agent, now, sent)
    assert asyncio.run(loop.tick()) is False                 # только что вошла: интервал не прошёл
    now[0] = 1099.0
    assert asyncio.run(loop.tick()) is False
    agent.busy = True
    now[0] = 1200.0
    assert asyncio.run(loop.tick()) is False                 # агент занят
    agent.busy = False
    assert asyncio.run(loop.tick()) is True and len(sent) == 1
    body = sent[0]
    assert body["kind"] == "self.work_tick" and "Игра" in body["text"] and body["data"] == {"module": "game"}
    assert asyncio.run(loop.tick()) is False                 # следующий не раньше интервала
    now[0] = 1301.0
    assert asyncio.run(loop.tick()) is True
    agent.last_end = 1400.0                                  # ход закончился недавно: свободному агенту не торопят
    now[0] = 1450.0
    assert asyncio.run(loop.tick()) is False


def test_work_loop_is_silent_outside_autonomous_environments_and_when_switched_off():
    now, sent = [5000.0], []
    agent = _Agent()
    for env, manifest, kwargs in (("assistant", WS_MANIFEST, {}), ("game", {"workspace": {"id": "game", "title": "x"}}, {}),
                                  ("game", WS_MANIFEST, {"settings": {"autoplay": False}}), ("game", WS_MANIFEST, {"quiet": True}),
                                  ("game", WS_MANIFEST, {"blocked": True})):
        loop = _loop([env], manifest, agent, now, sent, **kwargs)
        assert asyncio.run(loop.tick()) is False
        now[0] += 1000.0
        assert asyncio.run(loop.tick()) is False
    assert sent == []


def test_work_tick_goes_to_the_agent_through_the_think_route():
    from domain import rules
    assert rules.think_route(origin="self", kind="self.work_tick") is True
    assert rules.think_route(origin="external", kind="self.work_tick") is False


def test_manifest_workspace_accepts_autonomous_fields_and_rejects_bad_ones():
    from gits_sdk import protocol as P
    base = {"id": "game", "title": "G", "description": "d", "version": "1.0.0"}
    assert P.validate_manifest({**base, "workspace": {"id": "game", "title": "G", "autonomous": True, "work_interval_s": 90}}) == []
    bad = P.validate_manifest({**base, "workspace": {"id": "game", "title": "G", "autonomous": "да", "work_interval_s": 5}})
    assert any("autonomous" in p for p in bad) and any("work_interval_s" in p for p in bad)
