"""Среды модулей и вход по импульсу (ТЗ 32, этап B: WS-4, WS-5, WS-6, WS-12). Настоящее ядро (шина, внимание, менеджер сред и
фокуса) и модули-заглушки на клиенте SDK; игры, моделей и Claude нет.

Что проверяется: запуск игры не меняет среду, а лишь сигнал; вход по сообщению пользователя и по самоимпульсу (инструмент
`workspace.switch`); возврат в assistant по `done`, `blocked` и отключению; критический сигнал во время монтажа (и отказ в
небезопасной точке); старые имена запросов."""
import asyncio

import pytest
from focus_rigs import GAME_POLICIES, MONTAGE_POLICIES
from test_dashboard_endpoints import serve

from app import impulse_origin
from app import workspace_manager as wm
from domain.workspace import ASSISTANT, Reason
from focus import FocusManager
from environments.base import ToolError
from gits_sdk import ModuleClient
from module_hub import ModuleError


def manifest(module_id: str, policies: dict, workspace: bool = True, emits: tuple = ()) -> dict:
    m = {"id": module_id, "title": module_id.title(), "description": "Заглушка модуля.", "version": "1.0.0",
         "capabilities": ["game"] if module_id == "rimworld" else [], "subscribe": ["module"], "state": True,
         "emits": [{"kind": k, "description": "событие", "default_urgency": u} for k, u in emits],
         "policies": policies["policies"], "default_policy": policies["default_policy"]}
    if workspace:
        m["workspace"] = {"id": module_id, "title": module_id.title(), "purpose": "тест",
                          "entry": ["user_message", "self_impulse", "critical"], "signals_in": ["telegram.*"]}
    return m


class Stub:
    """Модуль-заглушка: журнал запросов среды и статус, который он присылает ядру."""

    def __init__(self, port, layer, module_id, policies, active_policy, **kw):
        self.client = ModuleClient(manifest(module_id, policies, **kw), url=f"ws://127.0.0.1:{port}/module/ws",
                                   token=layer.hub.token)
        self.id, self.active_policy, self.calls = module_id, active_policy, []
        self.interruptible, self.blocked = True, None
        for name in ("workspace.enter", "workspace.exit", "workspace.resume"):
            self.client.on_request(name)(self._handler(name))
        self.task = None

    def _handler(self, name):
        async def handler(body):
            self.calls.append(name)
            if name == "workspace.exit":
                return {"ok": True, "checkpoint": {"goal": body.get("reason", "")}}
            return {"ok": True, "policy": body.get("policy") or self.active_policy}
        return handler

    async def start(self):
        self.task = asyncio.create_task(self.client.run())
        await self.client.wait_connected(5)
        await self.report()

    async def report(self, **status):
        status = {"active_policy": self.active_policy, "interruptible": self.interruptible, **status}
        if self.blocked:
            status["blocked"] = self.blocked
        await self.client.state("заглушка", {"status": status})

    async def stop(self):
        await self.client.stop()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass


@pytest.fixture
async def world(tmp_path, monkeypatch):
    monkeypatch.setattr(wm, "GRACE_S", 0.0)                 # статусу модуля верим сразу: иначе тесты ждали бы реальные секунды
    port, layer, runner = await serve(tmp_path)
    stubs: list[Stub] = []

    async def add(module_id, policies, active, **kw):
        stub = Stub(port, layer, module_id, policies, active, **kw)
        stubs.append(stub)
        await stub.start()
        return stub

    layer.add = add
    try:
        yield layer
    finally:
        for stub in stubs:
            await stub.stop()
        await layer.stop()
        await runner.cleanup()


async def wait(pred, timeout=5.0):
    async def loop():
        while not pred():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(loop(), timeout)


async def enter_montage(layer, mont, **args):
    """Войти в монтаж по просьбе пользователя; модуль, приняв среду, переходит в активную политику (иначе статус «в фоне»
    означал бы «задача кончилась»)."""
    await tool(layer, "workspace.switch").call({"workspace": "montage", **args})
    mont.active_policy = "working"
    await mont.report()


def tool(layer, name):
    return next(t for t in layer.workspaces and layer_tools(layer) if t.id == name)


def layer_tools(layer):
    from environments import builtin
    return builtin._workspace_tools(layer.workspaces, layer.modules)


# ── WS-4: среды из манифестов ────────────────────────────────────────────────

async def test_workspaces_come_from_manifests_not_from_a_fixed_list(world):
    assert {s["id"] for s in world.workspaces.snapshot()["available"]} == {ASSISTANT, "sleep"}
    await world.add("rimworld", GAME_POLICIES, "afk")
    await world.add("plain", GAME_POLICIES, "afk", workspace=False)               # модуль без поля workspace средой не является
    ids = {s["id"] for s in world.workspaces.snapshot()["available"]}
    assert ids == {ASSISTANT, "sleep", "rimworld"}


# ── WS-5: вход только по импульсу ────────────────────────────────────────────

async def test_game_start_is_a_signal_and_does_not_change_the_workspace(world):
    game = await world.add("rimworld", GAME_POLICIES, "afk", emits=(("rimworld.game_started", 1),))
    await game.client.state("игра запущена", {"game_running": True, "status": {"active_policy": "afk", "interruptible": True}})
    await game.client.event("rimworld.game_started", "Игра RimWorld запущена.", urgency_hint=1)
    await wait(lambda: world.workspaces.signals["notify"] + world.workspaces.signals["digest"] >= 1 or world.workspaces.recent_signals)
    await asyncio.sleep(0.3)
    assert world.workspaces.current == ASSISTANT and game.calls == []
    assert world.profiles.current_id != "rimworld"


async def test_user_message_enters_the_module_workspace_and_the_profile_follows(world):
    game = await world.add("rimworld", GAME_POLICIES, "afk")
    res = await tool(world, "workspace.switch").call({"workspace": "rimworld", "goal": "зайти в колонию"})
    assert "Rimworld" in res
    assert (world.workspaces.current, world.workspaces.reason) == ("rimworld", Reason.USER)
    assert game.calls == ["workspace.enter"]
    assert world.profiles.current_id == "rimworld"                               # тон и инструменты следуют за средой
    assert world.focus.slot.module == "rimworld"
    status = await tool(world, "workspace.status").call({})
    assert "Rimworld" in status


async def test_self_impulse_enters_with_reason_self_and_an_ordinary_permission(world):
    await world.add("montage", MONTAGE_POLICIES, "idle")
    switch = tool(world, "workspace.switch")
    args = {"workspace": "montage", "goal": "собрать ролик"}
    assert switch.danger(args) is None                                           # по просьбе пользователя разрешение не нужно
    with impulse_origin.as_self():
        assert "собственному импульсу" in switch.danger(args)                    # по самоимпульсу роутер спросит разрешение
        await switch.call(args)
    assert (world.workspaces.current, world.workspaces.reason) == ("montage", Reason.SELF)


async def test_switch_refuses_sleep_and_unknown_workspaces(world):
    switch = tool(world, "workspace.switch")
    for ws in ("sleep", "minecraft"):
        with pytest.raises(ToolError):
            await switch.call({"workspace": ws})
    assert world.workspaces.current == ASSISTANT


async def test_switch_to_a_declared_but_unconnected_module_explains_why(world):
    world.modules.declare("rimworld", title="RimWorld")
    switch = tool(world, "workspace.switch")
    assert "rimworld" in switch.params["workspace"]["enum"]                      # планировщик не упирается в «одно из [assistant]»
    with pytest.raises(ToolError) as e:
        await switch.call({"workspace": "rimworld"})
    assert "RimWorld" in str(e.value) and "не подключился" in str(e.value)
    world.modules.is_enabled = lambda mid, cfg=None: False
    with pytest.raises(ToolError) as e:
        await switch.call({"workspace": "rimworld"})
    assert "выключен" in str(e.value) and "Модули" in str(e.value)
    assert world.workspaces.current == ASSISTANT


# ── возврат в assistant ──────────────────────────────────────────────────────

async def test_done_returns_to_assistant(world):
    mont = await world.add("montage", MONTAGE_POLICIES, "idle")
    await enter_montage(world, mont)
    await asyncio.sleep(0.2)
    assert world.workspaces.current == "montage"
    mont.active_policy = "idle"                                                  # задача кончилась: модуль вернулся в фон
    await mont.report()
    await wait(lambda: world.workspaces.current == ASSISTANT)
    assert world.workspaces.reason == Reason.DONE


async def test_blocked_returns_to_assistant(world):
    mont = await world.add("montage", MONTAGE_POLICIES, "idle")
    await enter_montage(world, mont)
    mont.blocked = "нет исходников"
    await mont.report()
    await wait(lambda: world.workspaces.current == ASSISTANT)
    assert world.workspaces.reason == Reason.BLOCKED and "исходников" in world.workspaces.history[-1]["note"]


async def test_module_disconnect_returns_to_assistant(world):
    game = await world.add("rimworld", GAME_POLICIES, "afk")
    await tool(world, "workspace.switch").call({"workspace": "rimworld"})
    await game.stop()
    await wait(lambda: world.workspaces.current == ASSISTANT, 8)
    assert world.workspaces.reason == Reason.DISCONNECT


# ── WS-6: сигналы вместо претензий на слот ───────────────────────────────────

async def test_foreign_events_are_signals_and_do_not_move_the_workspace(world):
    mont = await world.add("montage", MONTAGE_POLICIES, "idle")
    game = await world.add("rimworld", GAME_POLICIES, "afk", emits=(("rimworld.letter", 2),))
    await enter_montage(world, mont)
    for _ in range(3):
        await game.client.event("rimworld.letter", "Письмо: торговцы прибыли", urgency_hint=4)
    await wait(lambda: len(world.workspaces.recent_signals) >= 1)
    await asyncio.sleep(0.3)
    assert world.workspaces.current == "montage" and game.calls == []
    assert world.focus.slot.module == "montage"                                   # слот не достаётся претенденту по порогам
    assert mont.calls == ["workspace.enter"]


async def test_critical_signal_during_montage_switches_by_rules(world):
    mont = await world.add("montage", MONTAGE_POLICIES, "idle")
    game = await world.add("rimworld", GAME_POLICIES, "afk", emits=(("rimworld.raid", 5),))
    await enter_montage(world, mont, goal="ролик")
    await game.client.event("rimworld.raid", "Набег: трое рейдеров у ворот", urgency_hint=5)
    await wait(lambda: world.workspaces.current == "rimworld")
    assert world.workspaces.reason == Reason.CRITICAL
    assert mont.calls[-1] == "workspace.exit" and game.calls == ["workspace.enter"]
    assert world.workspaces.signals["switch"] == 1


async def test_critical_signal_is_refused_at_an_unsafe_point(world):
    mont = await world.add("montage", MONTAGE_POLICIES, "idle")
    game = await world.add("rimworld", GAME_POLICIES, "afk", emits=(("rimworld.raid", 5),))
    await enter_montage(world, mont)
    mont.interruptible = False                                                    # идёт сборка: выходить нельзя
    await mont.report()
    await game.client.event("rimworld.raid", "Набег", urgency_hint=5)
    await wait(lambda: world.workspaces.signals["refused"] == 1)
    assert world.workspaces.current == "montage" and game.calls == []


# ── совместимость FocusManager: новые имена с откатом на прежние ─────────────

class OldModule:
    """Соединение с модулем, который знает только прежние имена запросов."""

    def __init__(self):
        self.names = []

    async def request(self, name, body, timeout=0):
        self.names.append(name)
        if name.startswith("workspace."):
            raise ModuleError("unknown", "модуль не обрабатывает запрос")
        return {"ok": True, "policy": body.get("policy"), "checkpoint": {}}


@pytest.mark.parametrize("new_names, expected", [(True, ["workspace.enter", "policy.activate", "workspace.exit", "focus.release"]),
                                                 (False, ["policy.activate", "focus.release"])])
async def test_focus_manager_falls_back_to_the_old_request_names(new_names, expected):
    mod = OldModule()
    focus = FocusManager(connection_of=lambda m: mod, status_of=lambda m: None,
                         policies_of=lambda m: GAME_POLICIES, workspace_names=new_names, auto_switch=False)
    assert (await focus.request("rimworld"))["ok"]
    assert (await focus.release_holder())["ok"]
    assert mod.names == expected


async def test_focus_manager_without_auto_switch_ignores_events():
    focus = FocusManager(connection_of=lambda m: OldModule(), status_of=lambda m: None, policies_of=lambda m: GAME_POLICIES,
                         auto_switch=False)

    class Item:
        module, kind, text, urgency, data, expires, dedupe_key = "rimworld", "rimworld.raid", "набег", 5, {}, None, None

    focus.on_decided(Item(), "D4", 90.0)
    await focus.tick()
    assert focus.slot is None and not focus.events


async def test_reflection_do_intent_runs_the_planner_as_a_self_impulse(tmp_path):
    """Intent `do` размышления (ТЗ 30) отдаёт текст планировщику; всё, что тот вызовет, идёт с признаком самоимпульса."""
    from types import SimpleNamespace

    import impulse_service
    seen = []

    class FakeRouter:
        async def handle(self, text, announce=True):
            seen.append((text, impulse_origin.current()))

    companion = SimpleNamespace(_dir=str(tmp_path), _llm=None, memory_reader=None, memory_writer=None,
                                _settings=SimpleNamespace(load=lambda: {}), _context=lambda *a, **k: "")
    attention = SimpleNamespace(submit_self=None, reflect_speech=None, attach_reflection=lambda service: None)
    impulses = impulse_service.build(companion, attention, router=lambda: FakeRouter())
    await impulses.reflection.plan("зайди в колонию")
    assert seen == [("зайди в колонию", impulse_origin.SELF)]
    assert impulse_origin.current() == impulse_origin.USER                       # вне блока признак снят
