"""RimWorld под вниманием v2: виды из манифеста, служебные обработчики до решения, рабочий цикл `core.work_tick`, тревога прерывает
рабочий ход, хвосты инструкций. Без игры, модели и сети (настоящие `Attention`, `AttentionV2`, `ResidentRuntime`, `ResidentAgent`
на поддельной среде исполнения)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from environments.base import Tool
from hub_helpers import FakeCompanion, FakeIO
from services.attention import Attention
from services.attention_v2.attention import AttentionV2
from services.attention_v2.event import IMPORTANT, NORMAL, USER, Event, Sender
from services.attention_v2.eventlog import EventLog
from services.attention_v2.kinds import KindRegistry, KindSpec
from services.attention_v2.routing import (DEFAULT_PREFIXES, WORK_TICK_KIND, V2Router, register_core_kinds, work_tick_event)
from services.attention_v2.testing import FakeClock, Rig as AttentionRig
from services.conversation import resident
from services.event_handlers.game import make_colonists_handler, make_run_lost_hook
from services.reflection.work_loop import WorkLoop
from services.runtime import attention_wiring as aw
from services.runtime.turns import ALERT_HEADER, USER_TAIL, ResidentRuntime, is_alert, turn_kind
from test_resident_runtime import GateRuntime, Host, wait_until

RIMWORLD_AGENT = Path(__file__).resolve().parents[2] / "workspaces" / "rimworld" / "agent"


def rimworld_manifest_parts() -> tuple[list[dict], list[dict], dict]:
    """`event_kinds()`, `EMITS` и `EXPIRES_S` модуля RimWorld. Модуль подключается к sys.path на время импорта и убирает за собой свои
    модули (у него свои `settings` и `events_log`)."""
    before = set(sys.modules)
    sys.path.insert(0, str(RIMWORLD_AGENT))
    try:
        import core_link
        import module_manifest
        return (module_manifest.event_kinds(), json.loads(json.dumps(module_manifest.EMITS)), dict(core_link.EXPIRES_S))
    finally:
        sys.path.remove(str(RIMWORLD_AGENT))
        for name in set(sys.modules) - before:
            f = getattr(sys.modules[name], "__file__", None) or ""
            if str(RIMWORLD_AGENT) in str(f):
                del sys.modules[name]


def rimworld_registry() -> KindRegistry:
    reg = KindRegistry()
    register_core_kinds(reg)
    kinds, _, _ = rimworld_manifest_parts()
    reg.register_manifest("rimworld", {"event_kinds": kinds})
    return reg


def bus_event(kind: str, text: str, hint: int, **extra) -> dict:
    """Событие шины, как его шлёт `CoreLink.event`."""
    return {"kind": kind, "text": text, "urgency_hint": hint, "addressed_to_me": False, **extra}


# ---- манифест ------------------------------------------------------------------------------------------------------------

def test_manifest_event_kinds_cover_emits_and_follow_the_urgency_table():
    kinds, emits, expires = rimworld_manifest_parts()
    specs = {d["kind"]: KindSpec.from_dict(d) for d in kinds}
    assert set(specs) == {e["kind"] for e in emits}
    urgency = {e["kind"]: e["default_urgency"] for e in emits}
    raid = specs["rimworld.raid"]
    assert (raid.default_priority, raid.max_priority, raid.critical, raid.ttl_s) == (90, 90, True, 180.0)    # тревога 90, а не 100: это не человек
    assert all(s.max_priority < USER for s in specs.values())
    for kind, spec in specs.items():
        if not spec.passive:
            assert spec.default_priority == {5: 90, 4: 70, 3: 50, 2: 30, 1: 10}[urgency[kind]], kind
        assert spec.ttl_s == float(expires.get(kind, 120)), kind
    assert specs["rimworld.death"].default_priority == 70 and specs["rimworld.death"].critical
    assert specs["rimworld.fire"].critical and specs["rimworld.letter"].default_priority == 30
    assert specs["rimworld.run_ended"].critical and specs["rimworld.run_ended"].ttl_s == 1800.0
    # информационные виды сами не будят: потолок ниже WAKE_MIN и passive
    for kind in ("rimworld.colonists", "rimworld.game_started", "rimworld.run_video", "rimworld.enemy_down", "rimworld.research_done"):
        assert specs[kind].passive and specs[kind].max_priority < 30 and not specs[kind].critical, kind
    assert specs["rimworld.pawn_hurt"].coalesce and not specs["rimworld.raid"].coalesce
    assert rimworld_registry().kinds("rimworld") == sorted(specs)


# ---- маршрут и служебные обработчики --------------------------------------------------------------------------------------

class FakeV2:
    def __init__(self):
        self.events = []

    async def process_event(self, e):
        self.events.append(e)


def test_rimworld_prefix_is_routed_to_v2_by_default():
    assert "rimworld." in DEFAULT_PREFIXES


async def test_colonists_handler_and_run_ended_hook_still_run_before_v2_decision():
    legacy = Attention(FakeCompanion(), FakeIO(), idle_source=lambda: 0.0)
    synced, lost = [], []

    class Rel:
        def sync_colonists(self, pawns):
            synced.append(("rel", [p["name"] for p in pawns]))

    class World:
        def sync(self, pawns, colony):
            synced.append(("world", colony))

    legacy.register_handler("rimworld.colonists", make_colonists_handler(Rel(), World()))
    hook = make_run_lost_hook()

    async def on_run_ended(item):
        lost.append((item.data.get("outcome"), item.text))
        await hook(item)
    legacy.register_hook("rimworld.run_ended", on_run_ended)

    v2 = FakeV2()
    router = V2Router(v2, rimworld_registry(), DEFAULT_PREFIXES, side_effects=legacy.run_kind_handlers)
    await router.submit("rimworld", bus_event("rimworld.colonists", "Колония «Дом»: 2.", 0,
                                              data={"pawns": [{"name": "Борис"}, {"name": "Вика"}], "colony": "Дом"}))
    await router.submit("rimworld", bus_event("rimworld.run_ended", "Колония погибла", 5, data={"outcome": "colony_died"},
                                              expires_in_s=1800))
    await legacy.settle()
    assert synced == [("rel", ["Борис", "Вика"]), ("world", "Дом")]               # обработчик отработал
    assert lost == [("colony_died", "Колония погибла")]                           # хук отработал
    assert [e.kind for e in v2.events] == ["rimworld.run_ended"]                  # данные колонии v2 не нужны, итог забега идёт дальше
    ended = v2.events[0]
    assert ended.priority == 100 and ended.expires_at - ended.ts == 1800        # потолок 90 зажмёт его уже в process_event


async def test_side_effect_failure_does_not_stop_the_event():
    v2 = FakeV2()

    def broken(peer, event):
        raise RuntimeError("граф недоступен")
    router = V2Router(v2, rimworld_registry(), DEFAULT_PREFIXES, side_effects=broken)
    await router.submit("rimworld", bus_event("rimworld.raid", "Набег", 5))
    assert [e.kind for e in v2.events] == ["rimworld.raid"]


async def test_wire_attention_v2_passes_legacy_side_effects_and_work_tick_helpers():
    calls = []
    legacy = SimpleNamespace(run_kind_handlers=lambda peer, ev: calls.append(ev["kind"]) or True)
    layer = aw.wire_attention_v2(SimpleNamespace(resident=SimpleNamespace()), legacy, SimpleNamespace(manifest_of=lambda p: None),
                                 SimpleNamespace(current="rimworld"), SimpleNamespace())
    try:
        assert layer.router.side_effects is legacy.run_kind_handlers
        assert "rimworld." in layer.router.prefixes
        await layer.router.submit("rimworld", bus_event("rimworld.colonists", "список", 0))
        assert calls == ["rimworld.colonists"]
        assert layer.work_busy() is False
    finally:
        await layer.close()


# ---- рабочий цикл ------------------------------------------------------------------------------------------------------

def test_work_tick_kind_is_normal_replacing_and_expires_in_90s():
    reg = KindRegistry()
    register_core_kinds(reg)
    spec = reg.get(WORK_TICK_KIND)
    assert (spec.default_priority, spec.max_priority, spec.ttl_s, spec.coalesce, spec.replace) == (NORMAL, NORMAL, 90.0, True, True)
    assert spec.default_priority >= 30                                              # WAKE_MIN: тик будит свободного агента


async def test_second_tick_replaces_the_waiting_one_instead_of_piling_up():
    r = AttentionRig(focus="rimworld")
    r.registry.register(KindSpec(kind=WORK_TICK_KIND, default_priority=NORMAL, max_priority=NORMAL, ttl_s=90, coalesce=True,
                                 replace=True), "core")
    r.runtime.set_batch([r.ev(USER, text="человек")])                              # агент занят: тик ждёт в очереди
    first = work_tick_event("Рабочий цикл: первый", ts=r.clock.time())
    await r.attention.process_event(first)
    await r.attention.process_event(work_tick_event("Рабочий цикл: второй", ts=r.clock.time()))
    waiting = [e for e in r.attention.queue.items() if e.kind == WORK_TICK_KIND]
    assert len(waiting) == 1 and waiting[0].text == "Рабочий цикл: второй"


class WorkRig:
    """Настоящие `Attention v2`, `ResidentRuntime` и `ResidentAgent` с играми-заглушками: рабочий ход, тревога, следующий тик."""

    def __init__(self, scripts, focus="rimworld"):
        self.clock = FakeClock()
        self.model = GateRuntime(scripts)
        self.effect_calls: list[str] = []
        self.host = Host([])

        def game_tool(name, read_only=False):
            async def call(args):
                if not read_only:
                    self.effect_calls.append(name)
                return "готово"
            return Tool(id=f"rimworld.{name}", env="rimworld", name=name, description=name, call=call, read_only=read_only, risk="auto")
        for t in (game_tool("pause"), game_tool("colony_threats", read_only=True), game_tool("draft")):
            self.host.router.tools[t.id] = t
        self.agent = resident.ResidentAgent(self.host, runtime=self.model)
        self.stopped: list[str] = []

        async def stop_speech(why):
            self.stopped.append(why)
        self.runtime = ResidentRuntime(self.agent, stop_speech=stop_speech)
        self.registry = rimworld_registry()
        self.focus = focus
        self.attention = AttentionV2(self.registry, EventLog(clock=self.clock), self.runtime, self.clock, lambda: self.focus)
        self.runtime.attention_v2 = self.attention
        self.router = V2Router(self.attention, self.registry, DEFAULT_PREFIXES)

    async def tick(self, text="Рабочий цикл: ты в среде «RimWorld» и свободна."):
        await self.attention.process_event(work_tick_event(text, ts=self.clock.time()))
        await self.clock.advance(1)                                                                      # окно слияния COALESCE_MS (у тика есть ключ слияния)

    async def bus(self, kind, text, hint, **extra):
        await self.router.submit("rimworld", bus_event(kind, text, hint, **extra))

    async def idle(self):
        await wait_until(lambda: not self.runtime.current_batch() and not self.agent.busy)

    async def close(self):
        await self.runtime.close()
        self.attention.close()


async def test_raid_interrupts_work_tick_turn_and_next_tick_resumes_work():
    r = WorkRig([[("tool", "rimworld.pause"), ("hold",)], [("tool", "rimworld.colony_threats")], []])
    await r.tick()
    await wait_until(lambda: r.model.prompts and any(a.status == "done" for a in r.agent.turn_actions))
    assert resident.WORK_TICK_TAIL in r.model.prompts[0] and "core/work_tick" in r.model.prompts[0]      # рабочий ход с хвостом тика
    assert r.effect_calls == ["pause"]

    await r.bus("rimworld.raid", "Набег: рейдеры у ворот.", 5, expires_in_s=180)                         # 90 > 30 + 20: прерывает
    await wait_until(lambda: len(r.model.prompts) == 2)
    alert = r.model.prompts[1]
    assert r.model.aborts == 1 and r.stopped == ["interrupted"]
    assert f"=== {ALERT_HEADER} ===" in alert and resident.URGENT_TAIL in alert and resident.WORK_TICK_TAIL not in alert
    assert "rimworld/raid" in alert and "(прервано:" in alert and "успели: rimworld.pause" in alert       # журнал: действие с эффектом учтено
    assert r.runtime.notes and not any(e.kind == WORK_TICK_KIND for e in r.attention.queue.items())       # тик не вернулся: был эффект
    await r.idle()

    await r.tick()                                                                                       # после тревоги работа продолжается
    await wait_until(lambda: len(r.model.prompts) == 3)
    assert resident.WORK_TICK_TAIL in r.model.prompts[2] and "(прервано" not in r.model.prompts[2]
    await r.idle()
    await r.close()


async def test_alert_turn_uses_alert_source_so_tools_do_not_wait_for_speech():
    r = WorkRig([[("tool", "rimworld.draft")]])
    seen = []
    waited = []

    async def wait_speech(max_s=10.0):
        waited.append(max_s)
    r.host.wait_speech = wait_speech
    orig = r.agent._record_action

    def record(tool, effect, summary="", status="started"):
        seen.append(r.agent._source)
        return orig(tool, effect, summary, status)
    r.agent._record_action = record
    await r.bus("rimworld.raid", "Набег", 5)
    await wait_until(lambda: seen)
    await r.idle()
    assert seen[0] == "alert" and waited == []
    await r.close()


async def test_work_tick_turn_is_event_source_and_alone_not_an_alert():
    r = WorkRig([[("tool", "rimworld.colony_threats")]])
    seen = []
    orig = r.agent._record_action

    def record(tool, effect, summary="", status="started"):
        seen.append(r.agent._source)
        return orig(tool, effect, summary, status)
    r.agent._record_action = record
    await r.tick()
    await r.idle()
    assert seen[0] == "event" and resident.URGENT_TAIL not in r.model.prompts[0]
    await r.close()


async def test_critical_kind_outside_focus_breaks_focus_cap_and_ordinary_game_events_do_not():
    r = WorkRig([[("hold",)], [], [], []], focus="discord")                                                   # агент вне игры
    await r.tick()                                                                                       # событие ядра потолком фокуса не режется
    await wait_until(lambda: r.model.prompts)
    await r.bus("rimworld.pawn_mood", "Колонист на грани срыва.", 4)                                      # 70 вне фокуса зажато до 60 (60 > 30 + 20: прерывает)
    mood = r.attention.log.query(kind="rimworld.pawn_mood")
    assert any("вне фокуса" in x.reason and x.extra["after"] == 60 for x in mood)
    await wait_until(lambda: len(r.model.prompts) >= 2)                                                   # тик без действий вернулся в очередь и идёт следом
    await r.idle()
    n = len(r.model.prompts)
    await r.bus("rimworld.raid", "Набег", 5)                                                              # critical: потолок фокуса его не режет
    await wait_until(lambda: len(r.model.prompts) == n + 1)
    raid = r.attention.log.query(kind="rimworld.raid")
    assert not any("вне фокуса" in x.reason for x in raid) and raid[-1].priority == 90
    assert resident.URGENT_TAIL in r.model.prompts[n]
    await r.idle()
    await r.close()


async def test_background_game_events_do_not_wake_idle_agent_and_do_not_reach_digest():
    r = WorkRig([[]])
    await r.bus("rimworld.game_started", "Игра запущена.", 1)
    await r.bus("rimworld.research_done", "Исследование завершено.", 2)
    assert not r.runtime.current_batch() and len(r.attention.queue) == 2
    await r.clock.advance(60)                                                                            # ON_TURN_S: пассивные ход не запускают
    assert not r.runtime.current_batch() and r.attention.status()["launches"] == 0
    await r.close()


async def test_idle_alert_wakes_agent_with_urgent_tail():
    r = WorkRig([[]])
    await r.bus("rimworld.death", "Погиб колонист Борис.", 4)
    await wait_until(lambda: r.model.prompts)
    assert resident.URGENT_TAIL in r.model.prompts[0] and r.model.prompts[0].count("rimworld/death") == 1
    await r.idle()
    await r.close()


# ---- бюджет прерываний ---------------------------------------------------------------------------------------------------

async def test_critical_kind_bypasses_interrupt_budget_but_ordinary_events_do_not():
    r = AttentionRig(focus="rimworld", interrupt_budget=1)
    r.runtime.set_batch([r.ev(30, text="работа")])
    await r.send(80, text="обычное 1")                                                                   # бюджет 1 из 1 израсходован
    r.runtime.set_batch([r.ev(30, text="работа")])
    await r.send(80, text="обычное 2")                                                                   # без бюджета: в очередь
    assert r.attention.status()["interrupts"]["skipped_budget"] == 1
    r.attention.queue.pop(10)                                                                            # очередь пуста: сравнение только с ходом
    r.runtime.set_batch([r.ev(30, text="работа")])
    await r.send(70, workspace="rimworld", kind="rimworld.alert", text="рейд")                          # critical: проходит мимо бюджета
    status = r.attention.status()["interrupts"]
    assert status["total"] == 2 and status["by_reason"] == {"margin": 1, "critical": 1} and status["skipped_budget"] == 1
    assert any(e.kind == "rimworld.alert" for e in r.runtime.interrupts[-1][0])


async def test_critical_alert_storm_is_never_muted_by_budget():
    r = AttentionRig(focus="rimworld", interrupt_budget=3)
    for i in range(6):                                                                                   # шесть тревог подряд, каждая поверх рабочего хода
        r.runtime.set_batch([r.ev(30, text="работа")])
        await r.send(70, workspace="rimworld", kind="rimworld.alert", text=f"тревога {i}")
    assert r.attention.status()["interrupts"]["total"] == 6 and r.attention.status()["interrupts"]["skipped_budget"] == 0


# ---- рабочий цикл: условия WorkLoop --------------------------------------------------------------------------------------

def _loop(env, *, autoplay=True, busy=False, agent_busy=False, last_end=0.0, now=1000.0, emitted=None):
    agent = SimpleNamespace(busy=agent_busy, last_end=last_end)
    manifest = {"workspace": {"id": "rimworld", "title": "RimWorld", "autonomous": True, "work_interval_s": 120, "purpose": "играть"}}

    async def emit(body):
        emitted.append(body)
    return WorkLoop(env_of=lambda: env, manifest_of=lambda m: manifest if m == "rimworld" else None, agent=agent, emit=emit,
                    settings=lambda: {"autoplay": autoplay}, quiet=lambda: False, clock=lambda: now, busy=lambda: busy)


async def test_work_loop_emits_only_in_game_env_when_free_and_autoplay_on():
    out: list = []
    loop = _loop("rimworld", emitted=out)
    assert await loop.tick() is False                                                                    # вход в среду только что: ждём интервал
    loop._since["rimworld"] = 0.0
    assert await loop.tick() is True and out[0]["kind"] == "self.work_tick" and "Рабочий цикл" in out[0]["text"]
    for kw in ({"env": "assistant"}, {"env": "discord"}, {"env": "rimworld", "autoplay": False},
               {"env": "rimworld", "agent_busy": True}, {"env": "rimworld", "busy": True}):      # вне игры, без autoplay, занята: тишина
        out2: list = []
        env = kw.pop("env")
        lp = _loop(env, emitted=out2, **kw)
        lp._since["rimworld"] = 0.0
        assert await lp.tick() is False and out2 == [], (env, kw)
    out3: list = []
    lp = _loop("rimworld", emitted=out3, last_end=950.0)                                                 # только что закончила ход: не торопим
    lp._since["rimworld"] = 0.0
    assert await lp.tick() is False


async def test_wired_work_tick_emit_and_busy_see_running_turn_and_waiting_tick():
    model = GateRuntime([[("hold",)], []])
    agent = resident.ResidentAgent(Host([]), runtime=model)
    layer = aw.wire_attention_v2(SimpleNamespace(resident=agent), SimpleNamespace(), SimpleNamespace(manifest_of=lambda p: None),
                                 SimpleNamespace(current="rimworld"), SimpleNamespace())
    try:
        assert layer.work_busy() is False
        await layer.work_tick_emit({"kind": "self.work_tick", "text": "Рабочий цикл: первый", "urgency_hint": 1, "data": {"module": "rimworld"}})
        await wait_until(lambda: model.prompts)
        assert layer.work_busy() is True and resident.WORK_TICK_TAIL in model.prompts[0]                  # идёт ход по тику
        await layer.work_tick_emit({"text": "Рабочий цикл: второй"})                                    # 30 не прерывает 30: ждёт в очереди
        await layer.work_tick_emit({"text": "Рабочий цикл: третий"})
        waiting = [x for x in layer.attention.queue.items() if x.kind == WORK_TICK_KIND]
        assert len(waiting) == 1 and waiting[0].text == "Рабочий цикл: третий"                           # один тик, новый заменил старый
        model.gate.set()
        await wait_until(lambda: len(model.prompts) == 2)                                                 # ждавший тик запускается по концу хода
        assert "третий" in model.prompts[1] and "второй" not in model.prompts[1]
        await wait_until(lambda: not layer.work_busy() and not agent.busy)
    finally:
        await layer.close()


# ---- хвосты инструкций --------------------------------------------------------------------------------------------------

def _ev(kind, priority, workspace="rimworld", sender=None):
    return Event(workspace=workspace, kind=kind, text="x", priority=priority, sender=sender)


def test_tail_priority_user_over_alert_over_work_tick_over_plain():
    human = Sender(id="o", name="Маша", is_owner=True)
    tick, raid, plain = _ev(WORK_TICK_KIND, 30, "core"), _ev("rimworld.raid", 90), _ev("rimworld.letter", 30)
    user = _ev("dashboard.message", USER, "dashboard", human)
    assert turn_kind([tick, raid, user, plain]) == ("user", USER_TAIL, "ВХОД: события")
    assert turn_kind([tick, raid, plain]) == ("alert", resident.URGENT_TAIL, ALERT_HEADER)
    assert turn_kind([tick, plain]) == ("event", resident.WORK_TICK_TAIL, "ВХОД: события")
    assert turn_kind([plain]) == ("event", resident.EVENT_TAIL, "ВХОД: события")


def test_is_alert_excludes_humans_core_events_and_low_priority():
    assert is_alert(_ev("rimworld.death", 70)) and is_alert(_ev("rimworld.raid", 90))
    assert not is_alert(_ev("rimworld.letter", 50))                                                      # ниже IMPORTANT
    assert not is_alert(_ev("discord.dm", IMPORTANT, "discord", Sender(id="u", name="Вася")))            # сообщение человека
    assert not is_alert(_ev("core.timer", IMPORTANT, "core"))                                            # напоминание ядра
    assert not is_alert(_ev("dashboard.message", USER, "dashboard", Sender(id="o", name="я", is_owner=True)))
    assert is_alert(_ev("discord.message", IMPORTANT, "discord", Sender(id="b", name="бот", is_bot=True)))


async def test_work_tick_batch_with_a_human_message_gets_user_tail():
    r = WorkRig([[]])
    human = Event(workspace="dashboard", kind="dashboard.message", text="привет", priority=USER,
                  sender=Sender(id="o", name="пользователь", is_owner=True))
    await r.runtime.trigger([work_tick_event("тик"), human])
    await r.idle()
    assert "ответь по делу" in r.model.prompts[0] and resident.WORK_TICK_TAIL not in r.model.prompts[0]
    await r.close()
