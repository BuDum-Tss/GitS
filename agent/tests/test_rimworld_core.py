"""Ядро после перевода RimWorld на модуль (ТЗ 11: RW-2, RW-5, RW-9, RW-11, RW-14b, RW-18, RW-19, RW-21): приказ игроку-агенту
инструментом order модуля фокуса, подграф через graph.get, список колонистов событием, поиск папки модуля, запись в modules.yaml,
отсутствие старых каналов агента игры. Модуль игры в тестах это эхо-модуль на SDK с capabilities ["game"] и order."""
import asyncio
import socket
import sys
from pathlib import Path

import pytest
from aiohttp import web
from hub_helpers import running_client, running_core
from test_companion_attention import Rig as ConversationRig

import core_service
import dashboard
import module_dirs
import settings
from environments.registry import Registry
from glados_module import ModuleClient
from supervisor import Supervisor, load_specs

AGENT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def game_manifest(**extra) -> dict:
    base = {"id": "rimworld", "title": "RimWorld", "description": "Тестовая игра.", "version": "1.0.0",
            "capabilities": ["game"], "subscribe": ["profile", "module"], "state": True, "graph": True,
            "tools": [{"name": "order", "description": "Приказ игроку-агенту.", "params": {"text": {"type": "string"}},
                       "risk": "auto", "availability": "profile", "timeout_s": 15}],
            "emits": [{"kind": "rimworld.colonists", "description": "Список колонистов", "default_urgency": 0}]}
    base.update(extra)
    return base


def game_client(url, token, manifest=None, accept="построй"):
    client = ModuleClient(manifest or game_manifest(), url=url, token=token)
    client.orders = []

    @client.tool("order")
    async def order(args):
        client.orders.append(args["text"])
        return {"accepted": args["text"].startswith(accept)}

    @client.on_request("graph.get")
    async def graph(body):
        return {"nodes": [{"id": "agent", "label": "Агент"}, {"id": "build", "label": "Строительство", "current": True}],
                "edges": [{"from": "agent", "to": "build"}], "current": "build"}
    return client


class Wired:
    """Слой модулей на настоящем компаньоне и настоящем сервере шины."""

    def __init__(self, tmp_path, monkeypatch):
        self.rig = ConversationRig(tmp_path, monkeypatch, attention=False)
        self.companion = self.rig.companion
        self.port = free_port()
        self.converse: list[str] = []

        async def converse(text, **kw):
            self.converse.append(text)
            return False
        self.companion._converse = converse
        self.registry = Registry()
        self.layer = core_service.wire_modules(self.companion, self.registry, port=self.port, specs=[],
                                               battery=lambda: {"level": 1.0, "sleeping": False})
        self.companion.game_order = self.layer.game_order
        self.url = f"ws://127.0.0.1:{self.port}/module/ws"

    async def __aenter__(self):
        app = web.Application()
        self.layer.hub.attach(app)
        self.runner = web.AppRunner(app, shutdown_timeout=0.5)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        await self.layer.hub.start()
        return self

    async def __aexit__(self, *exc):
        await self.layer.attention.settle()
        await self.layer.attention.stop()
        await self.layer.hub.stop()
        await self.runner.cleanup()

    def client(self, **kw):
        return game_client(self.url, self.layer.hub.token, **kw)

    async def wait(self, predicate, timeout=5.0):
        async def waiter():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(waiter(), timeout)


# ── RW-11, RW-21: приказ модулю игры из on_user_message ─────────────────────────────────────────

async def test_order_goes_to_game_module_in_focus_and_skips_conversation(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        client = w.client()
        async with running_client(client, _core_of(w)):
            await client.state("игра: колония «Дом»", {"game_running": True})            # CORE-19: профиль rimworld по state модуля
            await w.wait(lambda: w.layer.profiles.current_id == "rimworld")
            await w.companion.on_user_message("построй стену у входа")
            assert client.orders == ["построй стену у входа"]
            assert w.converse == []                                                       # приказ принят игрой: разговор не нужен
            await w.companion.on_user_message("как тебе погода?")
            assert client.orders == ["построй стену у входа", "как тебе погода?"]
            assert w.converse == ["как тебе погода?"]                                     # игра отказала: обычный разговор


async def test_order_is_not_sent_when_game_module_is_not_in_focus(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        client = w.client()
        async with running_client(client, _core_of(w)):
            await client.state("игра: нет", {"game_running": False})
            await asyncio.sleep(0.2)
            assert w.layer.profiles.current_id != "rimworld"
            await w.companion.on_user_message("построй стену")
            assert client.orders == [] and w.converse == ["построй стену"]


async def test_order_skips_disabled_modules_modules_without_game_capability_or_order_tool(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        plain = w.client(manifest=game_manifest(capabilities=[]))
        async with running_client(plain, _core_of(w)):
            settings.update(profile="rimworld")
            w.layer.profiles.evaluate()
            assert await w.layer.game_order("построй стену") is False                     # нет capabilities game
            assert plain.orders == []
    async with Wired(tmp_path / "b", monkeypatch) as w:
        client = w.client()
        async with running_client(client, _core_of(w)):
            settings.update(profile="rimworld")
            w.layer.profiles.evaluate()
            assert await w.layer.game_order("построй стену") is True
            settings.update(modules={"rimworld": {"enabled": False}})
            w.layer.modules.invalidate()
            assert await w.layer.game_order("построй ещё") is False                       # модуль выключен: вызовов не передаём
            assert client.orders == ["построй стену"]
    async with Wired(tmp_path / "c", monkeypatch) as w:
        no_order = ModuleClient(game_manifest(tools=[]), url=w.url, token=w.layer.hub.token)
        async with running_client(no_order, _core_of(w)):
            settings.update(profile="rimworld")
            w.layer.profiles.evaluate()
            assert await w.layer.game_order("построй стену") is False                     # нет инструмента order


async def test_order_without_connected_module_is_false(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        settings.update(profile="rimworld")
        w.layer.profiles.evaluate()
        assert await w.layer.game_order("построй стену") is False
        await w.companion.on_user_message("построй стену")
        assert w.converse == ["построй стену"]


# ── RW-19, CORE-22: подграф профиля через graph.get ─────────────────────────────────────────────

async def test_profile_subgraph_comes_from_the_game_module(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        before = (await w.layer.profiles.graph())["subgraphs"]["rimworld"]
        assert before["live"] is False                                                     # модуля нет: запасной граф ядра
        client = w.client()
        async with running_client(client, _core_of(w)):
            live = (await w.layer.profiles.graph())["subgraphs"]["rimworld"]
            assert live["live"] is True and live["current"] == "build"
            assert {n["id"] for n in live["nodes"]} == {"agent", "build"}


# ── RW-9: список колонистов обновляет отношения и граф памяти ───────────────────────────────────

async def test_colonists_event_updates_relationships_and_graph(tmp_path):
    synced = []

    class Rel:
        def sync_colonists(self, pawns):
            synced.append(("rel", [p["name"] for p in pawns]))

    class Graph:
        async def sync_colonists(self, pawns, colony):
            synced.append(("graph", colony, [p["name"] for p in pawns]))

    async with running_core(tmp_path) as core:
        core.attention.kind_handlers["rimworld.colonists"] = core_service.make_colonists_handler(Rel(), Graph())
        client = game_client(core.url, core.token)
        async with running_client(client, core):
            await client.event("rimworld.colonists", "Колония «Дом»: колонистов 2.", urgency_hint=0, addressed_to_me=False,
                               data={"pawns": [{"name": "Борис"}, {"name": "Вика"}], "colony": "Дом"})
            await core.wait_for(lambda: len(synced) == 2)
            await core.attention.settle()
        assert synced == [("rel", ["Борис", "Вика"]), ("graph", "Дом", ["Борис", "Вика"])]
        assert core.attention.speech.items == [] and core.companion.about == []            # служебное событие не озвучивается
        assert core.attention.digest_counts() == {}                                        # и в сводку не попадает


async def test_event_handler_failure_does_not_break_the_hub(tmp_path):
    async with running_core(tmp_path) as core:
        async def broken(item):
            raise RuntimeError("граф недоступен")
        core.attention.kind_handlers["rimworld.colonists"] = broken
        client = game_client(core.url, core.token)
        async with running_client(client, core):
            await client.event("rimworld.colonists", "Колонисты.", urgency_hint=0, addressed_to_me=False, data={})
            await core.attention.settle()
            assert await client.request("core.persona", {})                                 # шина жива


# ── RW-2: папка модуля ──────────────────────────────────────────────────────────────────────────

def test_rimworld_dir_resolution_order(tmp_path):
    root = tmp_path / "GitS"
    root.mkdir()
    assert module_dirs.resolve_rimworld_dir({}, root) == (None, None)                       # ничего нет: «не настроен»
    (tmp_path / "rimworld-agent").mkdir()
    assert module_dirs.resolve_rimworld_dir({}, root) == (str(tmp_path / "rimworld-agent"), None)
    (tmp_path / "rimworld-module").mkdir()
    assert module_dirs.resolve_rimworld_dir({}, root) == (str(tmp_path / "rimworld-module"), None)   # новое имя папки раньше старого
    path, warning = module_dirs.resolve_rimworld_dir({"RIMWORLD_AGENT_DIR": r"D:\old"}, root)
    assert path == r"D:\old" and "RIMWORLD_MODULE_DIR" in warning                           # запасная старая переменная с предупреждением
    env = {"RIMWORLD_MODULE_DIR": r"D:\new", "RIMWORLD_AGENT_DIR": r"D:\old"}
    assert module_dirs.resolve_rimworld_dir(env, root) == (r"D:\new", None)                 # новая переменная раньше всего


def test_apply_sets_the_variable_for_modules_yaml(tmp_path, caplog):
    root = tmp_path / "GitS"
    root.mkdir()
    env = {"RIMWORLD_AGENT_DIR": str(tmp_path / "old")}
    with caplog.at_level("WARNING"):
        assert module_dirs.apply(env, root) == str(tmp_path / "old")
    assert env["RIMWORLD_MODULE_DIR"] == str(tmp_path / "old") and "устарела" in caplog.text
    empty = {}
    assert module_dirs.apply(empty, root) is None and "RIMWORLD_MODULE_DIR" not in empty


# ── RW-5: запись в modules.yaml ─────────────────────────────────────────────────────────────────

def test_shipped_modules_yaml_declares_the_rimworld_module(tmp_path, monkeypatch):
    spec = {s.id: s for s in load_specs()}["rimworld"]
    assert spec.kind == "module" and spec.autostart and spec.restart == "on-failure" and spec.enabled
    assert spec.command == "{PYTHON}" and spec.args == ["-u", "main.py"] and spec.cwd == "{RIMWORLD_MODULE_DIR}/agent"
    repo = tmp_path / "rimworld-module"
    (repo / "agent").mkdir(parents=True)
    (repo / "agent" / "main.py").write_text("", encoding="utf-8")
    sup = Supervisor([spec], token="t" * 64, core_url="ws://x", env={"RIMWORLD_MODULE_DIR": str(repo)})
    cmd, cwd = sup.resolve("rimworld")
    assert cmd == [sys.executable, "-u", "main.py"] and cwd == repo / "agent"                # общий интерпретатор ядра, без своего .venv
    other = Supervisor([spec], token="t" * 64, core_url="ws://x", env={})
    reason = other.resolve("rimworld")
    assert isinstance(reason, str) and "RIMWORLD_MODULE_DIR" in reason                     # нет переменной: «не настроен»


# ── RW-18: старых каналов агента игры больше нет ────────────────────────────────────────────────

def test_old_game_agent_channels_are_gone():
    app = dashboard.build_app()
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    for path in ("/api/say", "/api/speech_status", "/api/emotion", "/api/event", "/api/tool", "/api/web_search",
                 "/api/claude_usage"):
        assert not {m for m, p in routes if p == path}, path
    for kept in (("GET", "/api/modes"), ("GET", "/api/state"), ("POST", "/api/appraisal_label"), ("POST", "/api/menu_click")):
        assert kept in routes, kept
    assert not (AGENT / "game_bridge.py").exists()
    assert not (AGENT / "environments" / "manifests" / "rimworld.json").exists()
    assert not hasattr(dashboard, "set_game_link") and not hasattr(core_service, "_LINKED_SETTINGS")
    assert not hasattr(core_service, "post_run_summary")


def test_tray_no_longer_starts_the_game_agent():
    import tray
    for name in ("start_game_agent", "restart_game_agent", "GAME_AGENT_DIR", "game_agent_available"):
        assert not hasattr(tray, name), name


def test_record_runs_is_a_shared_setting_with_a_form_field():
    assert settings.DEFAULTS["record_runs"] is False
    html = (AGENT / "static" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "key: 'record_runs'" in html


# ── RW-8, RW-14b: дашборд берёт игру из состояния модуля, отметки оценок идут на адрес модуля ────────

async def test_dashboard_state_takes_the_game_from_module_state(tmp_path, monkeypatch):
    async with Wired(tmp_path, monkeypatch) as w:
        dashboard.set_modules(w.layer.modules, w.layer.hub, w.layer.supervisor)
        dashboard._MEMORY_DIR = tmp_path / "memory"
        try:
            offline = await dashboard.handle_api_state(None)
            import json
            assert json.loads(offline.body)["connected"] is False and json.loads(offline.body)["colony_name"] is None
            client = w.client()
            async with running_client(client, _core_of(w)):
                await client.state("игра: колония «Дом»", {"connected": True, "colony_name": "Дом", "tick": 7,
                                                           "pawns": [{"name": "Борис"}], "game_running": True})
                await w.wait(lambda: w.layer.modules.state_data("rimworld"))
                data = json.loads((await dashboard.handle_api_state(None)).body)
                assert data["connected"] is True and data["colony_name"] == "Дом" and data["pawns"] == [{"name": "Борис"}]
        finally:
            dashboard.set_modules(None, None, None)


def test_appraisal_label_url_new_variable_and_old_fallback(monkeypatch, caplog):
    monkeypatch.delenv("RIMWORLD_MODULE_URL", raising=False)
    monkeypatch.delenv("RIMWORLD_AGENT_URL", raising=False)
    assert dashboard._rimworld_url() == "http://127.0.0.1:8767"
    monkeypatch.setenv("RIMWORLD_AGENT_URL", "http://127.0.0.1:9000")
    with caplog.at_level("WARNING"):
        assert dashboard._rimworld_url() == "http://127.0.0.1:9000" and "устарела" in caplog.text
    monkeypatch.setenv("RIMWORLD_MODULE_URL", "http://127.0.0.1:9001")
    assert dashboard._rimworld_url() == "http://127.0.0.1:9001"


def _core_of(w):
    """running_client ждёт у «ядра» wait_connected(module_id): достаточно обёртки над шиной слоя."""
    class Core:
        async def wait_connected(self, module_id, timeout=5.0):
            await w.wait(lambda: w.layer.hub.connection(module_id) is not None, timeout)
    return Core()


@pytest.fixture(autouse=True)
def _reset_dashboard():
    yield
    dashboard.set_modules(None, None, None)
