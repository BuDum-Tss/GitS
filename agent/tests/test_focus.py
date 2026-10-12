"""Менеджер фокуса (ТЗ 18, FOC-2…FOC-9): протокол переключения, очередь, checkpoint, дребезг, пользовательские команды,
LLM-диспетчер и отказы. Заглушки модулей из focus_rigs.py; время подменяется, sleep нет."""
import asyncio
import json


import focus
from focus_rigs import (GAME_POLICIES, MONTAGE_POLICIES, TELEGRAM_POLICIES, World, decide, item, journal_reasons,
                        journal_types, run)

T0 = 1_000_000.0


def game_world():
    w = World(now=T0)
    w.add("rimworld", GAME_POLICIES)
    w.add("montage", MONTAGE_POLICIES)
    w.add("telegram", TELEGRAM_POLICIES, active="available")
    return w


def raid(module="rimworld", priority=90.0):
    return item(module, f"{module}.raid", "Набег: трое рейдеров у ворот", priority, urgency=5)


# ── протокол переключения ───────────────────────────────────────────────────

async def test_critical_event_switches_without_llm():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot is not None and mgr.slot.module == "rimworld"
    assert w.modules["rimworld"].active == "observe"
    assert w.llm_calls == []                                     # критическое событие не ждёт LLM
    assert w.types("rimworld") == ["policy.activate"]


async def test_low_priority_event_is_deferred_with_reason():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("rimworld", "rimworld.letter", "Игровое письмо", 40.0))
    await run(mgr, w)
    assert mgr.slot is None
    ev = next(iter(mgr.events.values()))
    assert ev.status == "deferred" and "ниже порога" in ev.reason
    assert w.calls == []


async def test_only_one_holder_and_non_critical_needs_margin():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    decide(mgr, item("montage", "montage.render_done", "Рендер готов", 70.0))
    await run(mgr, w, dt=25.0)                                   # запас 15 над фокусом 60: 70 его не даёт
    assert mgr.slot.module == "rimworld"
    assert "montage" not in w.types()


async def test_critical_event_releases_holder_before_activation():
    """Порядок протокола: release текущего модуля, затем activate нового."""
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    assert mgr.slot.module == "montage"
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"
    assert w.types() == ["policy.activate", "focus.release", "policy.activate"]
    assert [m for m, t, _ in w.calls] == ["montage", "montage", "rimworld"]


async def test_checkpoint_is_stored_before_next_activation():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    decide(mgr, raid())
    await run(mgr, w)
    types = journal_types(mgr)
    assert types.index("checkpoint") < len(types) - 1
    last_switch = max(i for i, t in enumerate(types) if t == "switch")
    assert types.index("checkpoint") < last_switch
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["status"] == "paused" and cp["payload"] == {"step": 3, "goal_text": "montage"}
    assert cp["resume"] == {"type": "holder_free"}


async def test_interrupted_task_resumes_when_holder_finishes():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    decide(mgr, raid())
    await run(mgr, w)
    w.now += 10
    w.modules["rimworld"].status["active_policy"] = "afk"        # бой кончился, игра вернулась в фон
    await run(mgr, w, dt=1.0)
    assert mgr.slot.module == "montage"
    assert "focus.resume" in w.types("montage")
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["status"] == "resumed"


async def test_resume_falls_back_to_policy_activate_for_old_module():
    """Модуль без focus.resume (RimWorld до ТЗ 18): возврат через policy.activate с checkpoint."""
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    decide(mgr, raid())
    await run(mgr, w)
    w.modules["montage"].unknown_resume = True
    w.now += 10
    w.modules["rimworld"].status["active_policy"] = "afk"
    await run(mgr, w, dt=1.0)
    activations = [b for m, t, b in w.calls if m == "montage" and t == "policy.activate"]
    assert activations[-1]["restored"] is True and activations[-1]["policy"] == "working"
    assert mgr.slot.module == "montage"


async def test_module_blocked_releases_focus_and_resumes_after_unblock():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    montage = w.modules["montage"]
    w.now += 10
    montage.status.update(active_policy="waiting_for_materials", blocked="нет исходников: clip_03.mp4")
    await run(mgr, w, dt=1.0)
    assert mgr.slot is None
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["status"] == "blocked" and "нет исходников" in cp["reason"]
    # пока блокировка не снята, задача не возобновляется
    await run(mgr, w, dt=1.0)
    assert mgr.slot is None
    montage.status.pop("blocked")
    montage.status["active_policy"] = "waiting_for_materials"
    await run(mgr, w, dt=1.0)
    assert mgr.slot is not None and mgr.slot.module == "montage"


async def test_refused_release_keeps_holder_and_event_queued():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    w.modules["rimworld"].refuse_release = True
    decide(mgr, item("montage", "montage.disaster", "Авария на линии", 90.0, urgency=5))
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"                          # модуль отказал: фокус остаётся
    ev = next(e for e in mgr.events.values() if e.module == "montage")
    assert ev.status == "deferred" and "не освободил фокус" in ev.reason
    # модуль согласился - событие переоценивается и переключает фокус
    w.modules["rimworld"].refuse_release = False
    w.now += focus.RECHECK_S + 1
    await run(mgr, w)
    assert mgr.slot.module == "montage"


async def test_refused_activation_keeps_event_and_resumes_previous_task():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    w.modules["montage"].refuse_activate = True
    decide(mgr, item("montage", "montage.disaster", "Авария на линии", 90.0, urgency=5))
    await run(mgr, w)
    # игра отпущена (checkpoint), переход монтажа отклонён: игра возвращается сама в этом же шаге
    assert mgr.slot is not None and mgr.slot.module == "rimworld"
    ev = next(e for e in mgr.events.values() if e.module == "montage")
    assert ev.status == "deferred" and "переход не удался" in ev.reason
    assert any(c["module"] == "rimworld" and c["status"] == "resumed" for c in mgr.checkpoints.values())


async def test_refusing_target_does_not_release_holder_again_at_once():
    """Модуль отказал в переходе: фокус прежнего владельца не отпускается повторно раньше REFUSE_BACKOFF_S."""
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    w.modules["montage"].refuse_activate = True
    decide(mgr, item("montage", "montage.disaster", "Авария на линии", 90.0, urgency=5))
    await run(mgr, w)
    releases = w.types("rimworld").count("focus.release")
    assert releases == 1 and mgr.slot.module == "rimworld"       # игра отпущена и возвращена один раз
    w.now += 5
    decide(mgr, item("montage", "montage.disaster2", "Вторая авария", 90.0, urgency=5))
    await run(mgr, w)
    assert w.types("rimworld").count("focus.release") == releases  # повторно не отпускаем
    assert mgr.slot.module == "rimworld"


async def test_module_without_policies_gets_no_focus():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("legacy", "legacy.ping", "Старый модуль", 95.0, urgency=5))
    await run(mgr, w)
    assert mgr.events == {} and mgr.slot is None


# ── очередь событий: состояния, дедупликация, старение, сроки ─────────────

async def test_same_event_within_window_is_merged():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.step", "Шаг 1", 40.0, dedupe="job-1"))
    await run(mgr, w)
    decide(mgr, item("montage", "montage.step", "Шаг 2", 45.0, dedupe="job-1"))
    await run(mgr, w)
    events = [e for e in mgr.events.values() if e.module == "montage"]
    assert len(events) == 1 and events[0].count == 2 and events[0].text == "Шаг 2"
    assert "merge" in journal_types(mgr)


async def test_aging_eventually_switches_a_deferred_event():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.idle_note", "Заметка монтажа", 40.0))
    for _ in range(40):                                          # 20 минут шагами по 30 с
        await run(mgr, w, dt=30.0)
        if mgr.slot is not None:
            break
    assert mgr.slot is not None and mgr.slot.module == "montage"
    assert "aging" in journal_types(mgr)


async def test_deferred_event_expires():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.note", "Старая заметка", 40.0, expires=T0 + 100))
    await run(mgr, w)
    w.now += 200
    await run(mgr, w)
    ev = next(iter(mgr.events.values()))
    assert ev.status == "expired" and "срок" in ev.reason


async def test_deferred_event_expires_after_ttl():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.note", "Заметка", 20.0))
    await run(mgr, w)
    w.now += focus.EVENT_TTL_S + 1
    await run(mgr, w)
    assert next(iter(mgr.events.values())).status == "expired"


async def test_dangerous_low_confidence_is_rechecked_and_not_lowered():
    w = game_world()
    w.recheck_result = None                                      # перепроверка не дала ответа
    mgr = w.manager(recheck=True)
    decide(mgr, item("rimworld", "rimworld.danger", "Возможно пожар", 50.0, category="danger", conf=0.2))
    await run(mgr, w)
    assert w.recheck_calls == 1
    assert "recheck" in journal_types(mgr)
    assert mgr.slot is not None and mgr.slot.module == "rimworld"   # опасное не понижено, переключило фокус
    assert "recheck" in journal_types(mgr)


async def test_recheck_result_sets_urgency():
    w = game_world()
    w.recheck_result = 5
    mgr = w.manager(recheck=True)
    decide(mgr, item("rimworld", "rimworld.danger", "Пожар в цехе", 50.0, category="danger", conf=0.1))
    await run(mgr, w)
    assert mgr.slot is not None and mgr.slot.module == "rimworld"


# ── дребезг (FOC-4) ─────────────────────────────────────────────────────────

async def test_debounce_min_focus_time_applies_to_non_critical():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    decide(mgr, item("montage", "montage.render_done", "Рендер", 95.0))
    w.now += 5.0                                                 # ещё нет MIN_FOCUS_S
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"
    assert any("дребезг" in r for r in journal_reasons(mgr, "defer"))
    w.now += focus.RECHECK_S + 1                                 # отложенное переоценивается раз в RECHECK_S
    await run(mgr, w)
    assert mgr.slot.module == "montage"


async def test_rate_limit_of_switches_per_minute():
    w = game_world()
    mgr = w.manager()
    for i in range(4):                                           # четыре переключения подряд критическими (дребезг их не держит)
        mod = "rimworld" if i % 2 == 0 else "montage"
        decide(mgr, item(mod, f"{mod}.alarm", "Тревога", 90.0, urgency=5))
        await run(mgr, w)
    before = mgr.slot.module
    other = "rimworld" if before == "montage" else "montage"
    decide(mgr, item(other, f"{other}.note", "Обычное", 95.0))
    w.now += 25.0                                                # минимум в фокусе прошёл, лимит в минуту - нет
    await run(mgr, w)
    assert mgr.slot.module == before
    assert any("лимит" in r for r in journal_reasons(mgr, "defer"))


async def test_unsafe_point_defers_non_critical_switch():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    w.modules["rimworld"].status["interruptible"] = False
    decide(mgr, item("montage", "montage.render_done", "Рендер", 95.0))
    w.now += 30.0
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"
    assert any("небезопасн" in r for r in journal_reasons(mgr, "defer"))


# ── пользователь: закрепить, «не беспокоить», разовое исключение (FOC-6) ───

async def test_user_request_bypasses_debounce_and_releases_holder():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    res = await mgr.request("montage", goal="смонтируй ролик")
    assert res["ok"] is True and mgr.slot.module == "montage" and mgr.slot.origin == "user"
    assert mgr.slot.priority == focus.USER_PRIORITY


async def test_pin_blocks_other_modules_but_not_critical():
    w = game_world()
    mgr = w.manager()
    res = await mgr.pin("rimworld")
    assert res["request"]["ok"] is True and mgr.slot.module == "rimworld"
    decide(mgr, item("montage", "montage.render_done", "Рендер", 95.0))
    w.now += 60.0
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"
    assert any("закреплён" in r for r in journal_reasons(mgr, "defer"))
    decide(mgr, raid("montage"))
    await run(mgr, w)
    assert mgr.slot.module == "montage"                          # критическое событие обходит закрепление
    await mgr.pin(None)
    assert mgr.pinned is None


async def test_dnd_for_module_defers_and_allow_once_passes_one_event():
    w = game_world()
    mgr = w.manager()
    await mgr.set_dnd("telegram", True)
    decide(mgr, item("telegram", "telegram.dm", "Сообщение от Маши", 80.0))
    await run(mgr, w)
    assert mgr.slot is None
    assert any("не беспокоить" in r for r in journal_reasons(mgr, "defer"))
    await mgr.allow_once("telegram")
    decide(mgr, item("telegram", "telegram.bot_message", "Ещё сообщение", 80.0))   # другой вид: не склеивается
    await run(mgr, w)
    # разовое исключение проходит к следующему сообщению: оно оценено по приоритету и переключило фокус;
    # первое сообщение по-прежнему в режиме «не беспокоить»
    assert mgr.slot is not None and mgr.slot.module == "telegram"
    assert mgr.dnd_modules == {"telegram"}                       # режим не отменён исключением
    assert mgr._once == set()
    first = next(e for e in mgr.events.values() if e.text == "Сообщение от Маши")
    assert first.status == "deferred" and "не беспокоить" in first.reason


async def test_dnd_all_is_respected_for_critical_only():
    w = game_world()
    mgr = w.manager()
    await mgr.set_dnd("all", True)
    decide(mgr, item("montage", "montage.render_done", "Рендер", 90.0))
    await run(mgr, w)
    assert mgr.slot is None
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"


async def test_busy_conversation_defers_non_critical_only():
    w = game_world()
    w.busy = True
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Рендер", 90.0))
    await run(mgr, w)
    assert mgr.slot is None and any("разговор" in r for r in journal_reasons(mgr, "defer"))
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"


async def test_transition_not_allowed_by_policy_is_deferred():
    w = game_world()
    montage = w.modules["montage"]
    montage.status["active_policy"] = "idle"
    montage.spec["policies"][0]["transitions"] = ["waiting_for_materials"]    # из простоя только в ожидание
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Рендер", 90.0, urgency=5))
    await run(mgr, w)
    assert mgr.slot is None
    assert any("не разрешён политикой" in r for r in journal_reasons(mgr, "defer"))


# ── LLM-диспетчер и отказы (FOC-7, отказ LLM, пункт 11 приёмки) ─────────────

async def test_llm_called_only_for_ambiguous_events():
    w = game_world()
    w.llm_reply = None
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Рендер", 40.0))   # далеко от порога: LLM не нужен
    await run(mgr, w)
    assert w.llm_calls == []
    decide(mgr, item("montage", "montage.render_done2", "Рендер 2", 62.0))  # у порога: неоднозначно
    await run(mgr, w)
    assert len(w.llm_calls) == 1
    ctx = w.llm_calls[0]
    assert set(ctx) >= {"event", "events", "modules", "holder", "checkpoints"}
    assert "snapshot" not in json.dumps(ctx, ensure_ascii=False)


async def test_llm_unavailable_rules_keep_working():
    w = game_world()
    w.llm_error = RuntimeError("модель недоступна")
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Рендер", 62.0))
    await run(mgr, w)
    assert "llm" in journal_types(mgr)                           # ошибка LLM записана, исключения наружу нет
    assert mgr.slot is not None and mgr.slot.module == "montage"  # правила: 62 без фокуса переключает (>= 60)
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"                         # критическое переключает и при отказе LLM


async def test_llm_timeout_is_treated_as_unavailable(monkeypatch):
    monkeypatch.setattr(focus, "LLM_TIMEOUT_S", 0.01)
    w = game_world()
    mgr = w.manager(llm=False)

    async def slow(ctx):
        await asyncio.sleep(1.0)
        return {"operations": []}

    mgr.llm = slow
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"                         # критическое не ждало LLM
    decide(mgr, item("montage", "montage.render_done", "Рендер", 62.0))
    await run(mgr, w)
    assert any(r["type"] == "llm" and r.get("status") == "unavailable" for r in mgr._journal)


async def test_llm_invalid_operations_are_rejected_and_do_not_switch():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    w.llm_reply = {"reason": "переключить на монтаж", "operations": [
        {"type": "request_policy_transition", "module": "ghost", "policy": "combat"},
        {"type": "request_policy_transition", "module": "montage", "policy": "idle"},   # фоновая: нельзя
        {"type": "explode_everything"},
        {"type": "reprioritize_event", "event_id": "нет", "priority": 99},
        {"type": "pause_task", "module": "montage"},                                    # не в фокусе
        "строка вместо объекта",
    ]}
    decide(mgr, item("montage", "montage.render_done", "Рендер", 62.0))
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"                         # ничего не переключено
    assert w.types("montage") == []
    rec = next(r for r in mgr._journal if r["type"] == "llm")
    assert len(rec["rejected"]) >= 5 and rec["accepted"] == []


async def test_llm_cannot_bypass_debounce():
    """Диспетчер может выбрать событие первым, но переключение проходит те же правила дребезга."""
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    mgr.slot.since = w.now                                       # только что передан

    async def propose(ctx):
        return {"reason": "переключить сейчас", "operations": [{"type": "select_task", "event_id": ctx["event"]["id"]}]}

    mgr.llm = propose
    decide(mgr, item("montage", "montage.render_done", "Рендер", 62.0))
    await run(mgr, w)
    assert mgr.slot.module == "rimworld"
    assert any("дребезг" in r for r in journal_reasons(mgr, "defer"))


async def test_critical_event_is_never_sent_to_llm():
    """Критическое событие не ждёт LLM и не может быть отложено диспетчером."""
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    assert mgr.slot.module == "rimworld" and w.llm_calls == []


async def test_llm_valid_reprioritize_is_applied():
    w = game_world()
    mgr = w.manager()

    async def dispatch(ctx):                                     # диспетчер знает id события из контекста
        return {"reason": "подождать", "operations": [
            {"type": "reprioritize_event", "event_id": ctx["event"]["id"], "priority": 45}]}

    mgr.llm = dispatch
    decide(mgr, item("montage", "montage.render_done", "Рендер", 62.0))
    await run(mgr, w)
    ev = next(iter(mgr.events.values()))
    assert ev.priority == 45 and mgr.slot is None                # 45 ниже порога: правила ждут
    assert "llm_op" in journal_types(mgr)


# ── отказ модуля и сохранение состояния (FOC-5, FOC-8) ───────────────────

async def test_checkpoint_survives_restart_on_disk(tmp_path):
    w = game_world()
    mgr = w.manager(tmp_path, state=True)
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    decide(mgr, raid())
    await run(mgr, w)
    await mgr._save()
    assert (tmp_path / "focus.json").exists()
    fresh = w.manager(tmp_path, state=True)
    fresh._load()
    cps = list(fresh.checkpoints.values())
    assert len(cps) == 1 and cps[0]["module"] == "montage" and cps[0]["status"] == "paused"
    assert cps[0]["scratch"] == "montage/scratch"


async def test_disconnected_holder_is_released_with_checkpoint():
    w = game_world()
    mgr = w.manager()
    decide(mgr, item("montage", "montage.render_done", "Монтаж начат", 80.0))
    await run(mgr, w)
    w.disconnect("montage")
    await run(mgr, w)
    assert mgr.slot is None
    cp = next(c for c in mgr.checkpoints.values() if c["module"] == "montage")
    assert cp["resume"] == {"type": "module_connected", "module": "montage"}


async def test_snapshot_explains_holder_queue_and_journal():
    w = game_world()
    mgr = w.manager()
    decide(mgr, raid())
    await run(mgr, w)
    decide(mgr, item("montage", "montage.note", "Заметка", 20.0))
    decide(mgr, item("telegram", "telegram.group", "Болтовня в группе", 20.0))
    await run(mgr, w)
    snap = mgr.snapshot()
    assert snap["holder"]["module"] == "rimworld" and snap["holder"]["policy"] == "observe"
    assert snap["queue"] and snap["queue"][0]["reason"]
    assert any(r["type"] == "switch" and r["reason"] for r in snap["journal"])
    assert snap["policies"]["rimworld"]["active"] == "observe"
    assert snap["policies"]["telegram"]["policies"][1]["quiet"] is True


async def test_stop_saves_state(tmp_path):
    w = game_world()
    mgr = w.manager(tmp_path, state=True)
    await mgr.pin("rimworld")
    await mgr.stop()
    data = json.loads((tmp_path / "focus.json").read_text(encoding="utf-8"))
    assert data["pin"] == "rimworld" and data["v"] == 1


async def test_core_without_modules_runs_ticks():
    w = World(now=T0)
    mgr = w.manager()
    await run(mgr, w, steps=3)                                   # пустая очередь, никаких модулей - без ошибок
    assert mgr.snapshot()["holder"] is None
