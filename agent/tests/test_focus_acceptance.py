"""Одиннадцать критериев приёмки ТЗ 18 (раздел 15 исходного документа) на заглушках модулей (FOC-10).

Telegram с «не беспокоить», игра с боем, монтаж с блокировкой материалов. Внимание настоящее (Rig из test_attention):
событие идёт через Attention.submit и хук on_decided, как в ядре. Claude и LM Studio не вызываются."""
from glados_module import protocol as P

from focus_rigs import GAME_POLICIES, MONTAGE_POLICIES, TELEGRAM_POLICIES, World, journal_types
from test_attention import Rig


GAME_CONN_MODULES = ("rimworld", "montage", "telegram")


def wire(tmp_path, *, llm=True):
    """Внимание (настоящее) + менеджер фокуса на заглушках модулей."""
    rig = Rig(tmp_path)
    world = World(now=rig.clock.t)
    world.add("rimworld", GAME_POLICIES)
    world.add("montage", MONTAGE_POLICIES)
    tg = world.add("telegram", TELEGRAM_POLICIES, active="available")
    assert tg.active == "available"
    rig.modules.declare("rimworld", emits=[("rimworld.raid", 5), ("rimworld.letter", 2)])
    rig.modules.declare("telegram", emits=[("telegram.dm", 3)])
    rig.modules.declare("montage", emits=[("montage.disaster", 5), ("montage.render_done", 2)])
    mgr = world.manager(llm=llm)
    rig.att.on_decided = mgr.on_decided
    return rig, world, mgr


async def send(rig, module, **event):
    """Событие модуля через настоящее внимание (правила, формула, решение D0–D4, хук фокуса)."""
    event.setdefault("kind", f"{module}.ping")
    event.setdefault("text", "Событие")
    return await rig.submit(module, **event)


async def step(mgr, world, n=1, dt=0.0):
    for _ in range(n):
        await mgr.tick()
        world.now += dt


# 1. Каждый модуль объявляет собственные политики и допустимые переходы.
def test_c1_each_module_declares_policies_and_transitions():
    for spec in (GAME_POLICIES, MONTAGE_POLICIES, TELEGRAM_POLICIES):
        manifest = {"id": "demo", "title": "Demo", "description": "d", "version": "1.0.0",
                    "policies": spec["policies"], "default_policy": spec["default_policy"]}
        assert P.validate_manifest(manifest) == []
        ids = {p["id"] for p in spec["policies"]}
        for pol in spec["policies"]:
            assert set(pol.get("transitions") or []) <= ids
    # игра, монтаж и Telegram: переходы описаны; у каждого фоновая политика по умолчанию
    assert {p["id"] for p in MONTAGE_POLICIES["policies"] if p["focus"] == "active"} == {"working"}


# 2. Одновременно активна не более чем одна основная задача (слот фокуса).
async def test_c2_at_most_one_active_focus(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "montage", kind="montage.disaster", text="Авария на линии", urgency_hint=5)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world, 2)
    active = [m for m in GAME_CONN_MODULES if m in world.modules
              and any(p["id"] == world.modules[m].active and p["focus"] == "active"
                      for p in world.modules[m].spec["policies"])]
    assert len(active) <= 1
    assert mgr.slot is not None and active == [mgr.slot.module]


# 3. События из разных модулей попадают в общую очередь и имеют приоритеты.
async def test_c3_events_from_modules_share_queue_with_priorities(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "telegram", kind="telegram.dm", text="Сообщение от Маши", urgency_hint=4)
    await send(rig, "rimworld", kind="rimworld.letter", text="Письмо", urgency_hint=2)
    await send(rig, "montage", kind="montage.render_done", text="Рендер готов", urgency_hint=2)
    await step(mgr, world)
    mods = {e.module for e in mgr.events.values()}
    assert mods == {"telegram", "rimworld", "montage"}
    assert all(isinstance(e.priority, float) and e.priority >= 0 for e in mgr.events.values())
    assert rig.att.on_decided is not None


# 4. При «не беспокоить» обычное сообщение Telegram не переключает фокус, но сохраняется.
async def test_c4_telegram_do_not_disturb_keeps_message_without_switch(tmp_path):
    rig, world, mgr = wire(tmp_path)
    tg = world.modules["telegram"]
    tg.status["active_policy"] = "do_not_disturb"                 # политика Telegram «не беспокоить»
    await send(rig, "telegram", kind="telegram.dm", text="Ты скоро?", urgency_hint=4)
    await step(mgr, world)
    assert mgr.slot is None and world.types("telegram") == []
    stored = [e for e in mgr.events.values() if e.module == "telegram"]
    assert len(stored) == 1 and stored[0].status == "deferred" and "не переключает фокус" in stored[0].reason
    # после выхода из режима сообщение оценивается заново (не теряется)
    tg.status["active_policy"] = "available"
    await step(mgr, world)
    assert stored[0].status in ("pending", "deferred", "resolved", "expired")
    assert stored[0].status != "discarded"


# 5. Критическое событие может вызвать переключение по заданным правилам.
async def test_c5_critical_event_switches_by_rules(tmp_path):
    rig, world, mgr = wire(tmp_path, llm=True)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег у ворот", urgency_hint=5)
    await step(mgr, world)
    assert mgr.slot is not None and mgr.slot.module == "rimworld"
    assert world.llm_calls == []                                  # без LLM


# 6. Перед переключением состояние прерываемой задачи сохраняется.
async def test_c6_checkpoint_saved_before_interrupted_task_released(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "montage", kind="montage.render_done", text="Монтаж начат", urgency_hint=5)
    await step(mgr, world)
    assert mgr.slot.module == "montage"
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world)
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["payload"] is not None and cp["status"] == "paused"
    types = [(m, t) for m, t, _ in world.calls]
    assert types.index(("montage", "focus.release")) < types.index(("rimworld", "policy.activate"))


# 7. После срочной задачи Агент восстанавливает предыдущую задачу, если она актуальна.
async def test_c7_previous_task_resumed_after_urgent_one(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "montage", kind="montage.render_done", text="Монтаж начат", urgency_hint=5)
    await step(mgr, world)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world)
    world.now += 10
    world.modules["rimworld"].status["active_policy"] = "afk"     # срочная задача закончилась
    await step(mgr, world, dt=1.0)
    assert mgr.slot is not None and mgr.slot.module == "montage"
    assert "focus.resume" in world.types("montage")


# 8. Модуль сообщает о блокировке (нет материалов для монтажа) и освобождает фокус.
async def test_c8_module_reports_block_and_releases_focus(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "montage", kind="montage.render_done", text="Монтаж начат", urgency_hint=5)
    await step(mgr, world)
    world.now += 10
    world.modules["montage"].status.update(active_policy="waiting_for_materials",
                                            blocked="нет исходника clip_03.mp4")
    await step(mgr, world, dt=1.0)
    assert mgr.slot is None
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["status"] == "blocked" and "clip_03" in cp["reason"]


# 9. Некорректное предложение LLM не приводит к недопустимому переходу.
async def test_c9_invalid_llm_proposal_causes_no_illegal_transition(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world)
    world.llm_reply = {"reason": "переключить", "operations": [
        {"type": "request_policy_transition", "module": "montage", "policy": "idle"},   # фоновая политика
        {"type": "request_policy_transition", "module": "ghost", "policy": "working"},   # нет модуля
        {"type": "reprioritize_event", "event_id": "нет-такого", "priority": 99},
    ]}
    await send(rig, "montage", kind="montage.render_done", text="Рендер", urgency_hint=3)
    await step(mgr, world)
    assert mgr.slot.module == "rimworld"
    assert world.types("montage") == []
    assert [r for r in mgr._journal if r["type"] == "llm"][-1]["accepted"] == []


# 10. По журналу можно установить причину выбора, откладывания, прерывания и возобновления задачи.
async def test_c10_journal_explains_select_defer_interrupt_resume(tmp_path):
    rig, world, mgr = wire(tmp_path)
    await send(rig, "montage", kind="montage.render_done", text="Монтаж начат", urgency_hint=5)
    await step(mgr, world)
    await send(rig, "telegram", kind="telegram.dm", text="Сообщение", urgency_hint=2)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world)
    world.now += 10
    world.modules["rimworld"].status["active_policy"] = "afk"
    await step(mgr, world, dt=1.0)
    reasons = {r["type"]: r.get("reason", "") for r in mgr._journal}
    for typ in ("switch", "defer", "checkpoint", "resume"):
        assert typ in journal_types(mgr), typ
    assert all(r.get("reason") for r in mgr._journal if r["type"] in ("switch", "resume"))
    assert reasons["switch"]


# 11. При недоступности LLM базовые правила обработки событий и критических прерываний продолжают работать.
async def test_c11_rules_work_without_llm(tmp_path):
    rig, world, mgr = wire(tmp_path)
    world.llm_error = RuntimeError("LM Studio недоступна")
    await send(rig, "montage", kind="montage.render_done", text="Рендер", urgency_hint=3)
    await step(mgr, world)
    await send(rig, "rimworld", kind="rimworld.raid", text="Набег", urgency_hint=5)
    await step(mgr, world)
    assert mgr.slot is not None and mgr.slot.module == "rimworld"
    assert "llm" in journal_types(mgr) or world.llm_calls == []


async def test_attention_hook_is_called_for_decided_events(tmp_path):
    """Сквозной путь: решение внимания (D2 при срочности 2) доходит до очереди фокуса через хук on_decided."""
    rig, world, mgr = wire(tmp_path)
    decision = await send(rig, "rimworld", kind="rimworld.letter", text="Письмо", urgency_hint=2)
    assert decision is not None and decision["decision"] in ("D1", "D2", "D3", "D4")
    await step(mgr, world)
    queued = [e for e in mgr.events.values() if e.module == "rimworld" and e.text == "Письмо"]
    assert len(queued) == 1 and queued[0].status == "deferred"


async def test_legacy_module_without_policies_is_not_queued(tmp_path):
    """Режим совместимости: модуль без policies работает как раньше, в фокусе не участвует."""
    rig, world, mgr = wire(tmp_path)
    rig.modules.declare("echo", emits=[("echo.ping", 5)])
    await send(rig, "echo", kind="echo.ping", text="Пинг", urgency_hint=5)
    await step(mgr, world)
    assert all(e.module != "echo" for e in mgr.events.values())
    assert mgr.slot is None
