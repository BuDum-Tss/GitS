"""Внимание v2: критерии приёмки 1, 2, 3, 5, 6, 7, 9 раздела 12 RUNTIME_SPEC, фокус, USER-ход, разовый таймер. Без модели и реального времени."""
from services.attention_v2.event import USER, EventState
from services.attention_v2.testing import Rig


def prios(events):
    return [e.priority for e in events]


# ---- 1. простаивающий агент ------------------------------------------------------------------------------------------

async def test_idle_low_priority_does_not_wake_but_40_does():
    r = Rig()
    low = await r.send(20)
    assert r.runtime.triggers == []                       # 20 < WAKE_MIN: ждёт on_turn
    assert low in r.attention.queue
    r2 = Rig()
    await r2.send(40)
    assert len(r2.runtime.triggers) == 1 and prios(r2.runtime.last_trigger) == [40]


# ---- 2. очередь [50, 30] и простой ------------------------------------------------------------------------------------

async def test_idle_queue_pack_on_higher_event_and_lower_goes_to_queue():
    r = Rig()
    r.attention.queue.push(r.ev(50))
    r.attention.queue.push(r.ev(30))
    await r.send(60)
    assert prios(r.runtime.last_trigger) == [60, 50, 30]
    assert len(r.attention.queue) == 0

    r = Rig()
    r.attention.queue.push(r.ev(50))
    r.attention.queue.push(r.ev(30))
    e = await r.send(40)                                   # 40 не выше максимума очереди 50
    assert r.runtime.triggers == []
    assert e in r.attention.queue and len(r.attention.queue) == 3


# ---- 3. занятый ход -----------------------------------------------------------------------------------------------------

async def test_busy_interrupt_rules():
    r = Rig()
    r.runtime.set_batch([r.ev(80)])
    await r.send(90)                                       # нужно строго больше 100
    assert r.runtime.interrupts == []
    await r.send(USER)
    assert len(r.runtime.interrupts) == 1

    r = Rig()
    r.runtime.set_batch([r.ev(40)])
    e55 = await r.send(55)                                 # 55 <= 60: в очередь
    assert r.runtime.interrupts == [] and e55 in r.attention.queue
    e60 = await r.send(60)                                 # ровно запас: не прерывает (строго больше)
    assert r.runtime.interrupts == [] and e60 in r.attention.queue
    r2 = Rig()
    r2.runtime.set_batch([r2.ev(40)])
    await r2.send(70)
    assert len(r2.runtime.interrupts) == 1 and prios(r2.runtime.interrupts[0][0]) == [70]


async def test_busy_compares_with_queue_max_too():
    r = Rig()
    r.attention.queue.push(r.ev(80))                       # в очереди 80, идёт ход 40: 70 не превосходит 80 + 20
    r.runtime.set_batch([r.ev(40)])
    await r.send(70)
    assert r.runtime.interrupts == []


async def test_interrupt_pack_is_new_event_plus_best_of_queue():
    r = Rig(events_batch=3)
    for p in (10, 35, 20, 45):
        r.attention.queue.push(r.ev(p))
    r.runtime.set_batch([r.ev(5)])
    await r.send(USER)
    new, old = r.runtime.interrupts[0]
    assert prios(new) == [100, 45, 35] and prios(old) == [5]


# ---- 5. шторм прерываний ------------------------------------------------------------------------------------------------

async def test_interrupt_storm_budget_and_user_passes():
    r = Rig()
    for _ in range(3):
        r.runtime.set_batch([r.ev(20)])
        await r.send(70)
    assert len(r.runtime.interrupts) == 3
    r.runtime.set_batch([r.ev(20)])
    fourth = await r.send(70)
    assert len(r.runtime.interrupts) == 3 and r.runtime.steers == [fourth]      # бюджет прерываний исчерпан: важное доливается в идущий ход
    st = r.attention.status()["interrupts"]
    assert st["total"] == 3 and st["skipped_budget"] == 1 and st["budget"]["used"] == 3

    r.runtime.set_batch([r.ev(20)])
    await r.send(USER)                                     # USER проходит и при исчерпанном бюджете
    assert len(r.runtime.interrupts) == 4

    await r.clock.advance(61)                              # окно 60 с прошло: бюджет снова есть
    assert r.attention.status()["interrupts"]["budget"]["used"] == 0
    r.runtime.set_batch([r.ev(20)])
    await r.send(70)
    assert len(r.runtime.interrupts) == 5


# ---- 6. слияние -------------------------------------------------------------------------------------------------------

async def test_coalesce_three_phrases_in_window():
    r = Rig()
    a = await r.send(40, text="раз", group_key="g:1")
    await r.clock.advance(0.1)
    await r.send(60, text="два", group_key="g:1")
    await r.clock.advance(0.1)
    await r.send(50, text="три", group_key="g:1")
    assert r.runtime.triggers == []                        # окно ещё идёт
    await r.clock.advance(0.19)                            # 390 мс от первой фразы: окно 400 мс не вышло
    assert r.runtime.triggers == []
    await r.clock.advance(0.02)
    assert len(r.runtime.triggers) == 1
    batch = r.runtime.last_trigger
    assert len(batch) == 1 and batch[0] is a
    assert batch[0].text == "раз два три" and batch[0].priority == 60
    assert r.attention.status()["merged"] == 2
    assert r.runtime.triggers[0][1] == 0


async def test_coalesce_merges_with_event_still_in_queue_and_raises_priority():
    r = Rig()
    a = await r.send(10, text="тихо", group_key="g:2")      # ниже WAKE_MIN: просто в очереди
    assert r.runtime.triggers == []
    await r.clock.advance(5)
    await r.send(100, text="ответь", group_key="g:2")       # слияние подняло приоритет: пора будить (с окном слияния)
    await r.clock.advance(0.5)
    assert len(r.runtime.triggers) == 1 and r.runtime.last_trigger == [a]
    assert a.text == "тихо ответь" and a.priority == 100


async def test_no_coalesce_for_different_group_or_kind_without_flag():
    r = Rig()
    await r.send(40, group_key="g:a")
    await r.send(40, group_key="g:b")
    await r.send(40, kind="test.plain", group_key="g:a")    # вид без coalesce не сливается
    await r.clock.advance(1)
    assert sum(len(b) for b, _ in r.runtime.triggers) == 3 and r.attention.status()["merged"] == 0


async def test_group_event_without_waiting_peer_starts_after_window():
    r = Rig()
    await r.send(USER, group_key="g:v")
    assert r.runtime.triggers == []
    await r.clock.advance(0.399)
    assert r.runtime.triggers == []
    await r.clock.advance(0.002)
    assert len(r.runtime.triggers) == 1


# ---- 7. срок ---------------------------------------------------------------------------------------------------------

async def test_expired_not_in_batch_and_goes_to_digest():
    r = Rig()
    old = await r.send(20, expires_at=r.clock.time() + 10)
    await r.send(20, text="живое")
    await r.clock.advance(15)                               # старое протухло, разовый таймер ещё не сработал (20 с)
    await r.runtime.finish_turn()                           # on_turn: сначала expire
    assert old.state is EventState.EXPIRED
    assert old not in r.attention.queue
    assert r.attention.digest.total == 1
    assert r.attention.status()["expired"] == 1
    await r.clock.advance(6)                                # дошло до ON_TURN_S у оставшегося
    batch = r.runtime.last_trigger
    assert [e.text for e in batch] == ["живое"]
    text = r.attention.render_batch(batch)
    assert text.splitlines()[0].startswith("(пропущено: 1 test/ev")


async def test_already_expired_event_is_not_queued():
    r = Rig()
    e = await r.send(80, expires_at=r.clock.time() - 1)
    assert e.state is EventState.EXPIRED and r.runtime.triggers == [] and len(r.attention.queue) == 0
    assert r.attention.digest.total == 1


async def test_evicted_on_overflow_goes_to_digest():
    r = Rig(queue_max=3)
    low = await r.send(10, text="a")
    await r.send(20)
    await r.send(25)
    await r.send(28)                                        # четвёртое: вытесняется самое слабое и старое
    assert low.state is EventState.EVICTED and len(r.attention.queue) == 3
    assert r.attention.status()["evicted"] == 1 and r.attention.digest.total == 1
    assert "1 test/ev" in r.attention.digest.render()


# ---- 9. on_turn ----------------------------------------------------------------------------------------------------------

async def test_on_turn_batches_low_events_after_on_turn_s():
    r = Rig()
    for i in range(3):
        await r.send(20, text=f"e{i}")
        await r.clock.advance(1)
    assert r.runtime.triggers == []
    await r.clock.advance(16.9)                             # 19.9 с с прихода первого
    assert r.runtime.triggers == []
    await r.clock.advance(0.2)                              # 20 с: разовый таймер
    assert len(r.runtime.triggers) == 1
    assert [e.text for e in r.runtime.last_trigger] == ["e0", "e1", "e2"]


async def test_on_turn_at_turn_end_launches_by_max_and_re_arms_timer():
    r = Rig()
    r.runtime.set_batch([r.ev(80)])
    await r.send(35)                                        # занят: в очередь
    await r.send(30)
    r.runtime.running = []
    await r.attention.on_turn_end()                         # максимум 35 >= WAKE_MIN: сразу пачкой
    assert prios(r.runtime.last_trigger) == [35, 30]

    r = Rig()
    r.runtime.set_batch([r.ev(80)])
    await r.send(15)
    await r.runtime.finish_turn()                           # 15 < WAKE_MIN и ждёт меньше 20 с: не запускает, ставит таймер
    assert r.runtime.triggers == []
    await r.clock.advance(20)
    assert prios(r.runtime.last_trigger) == [15]


async def test_timer_is_one_shot_not_a_tick():
    r = Rig()
    await r.send(20)
    await r.clock.advance(20)
    assert len(r.runtime.triggers) == 1
    await r.runtime.finish_turn()
    await r.clock.advance(600)
    assert len(r.runtime.triggers) == 1                     # очередь пуста: ничего не повторяется
    assert r.attention.status()["empty_on_turn"] >= 1


async def test_on_turn_idle_proactive_off_by_default_and_on_when_enabled():
    r = Rig()
    await r.attention.on_turn()
    assert r.runtime.idle_triggers == 0
    state = {"idle": True, "pro": True}
    r = Rig(idle_long_enough=lambda: state["idle"], proactive_enabled=lambda: state["pro"])
    await r.attention.on_turn()
    assert r.runtime.idle_triggers == 1
    state["pro"] = False
    await r.attention.on_turn()
    assert r.runtime.idle_triggers == 1


async def test_on_turn_does_nothing_while_busy():
    r = Rig()
    r.runtime.set_batch([r.ev(80)])
    r.attention.queue.push(r.ev(50))
    await r.attention.on_turn()
    assert r.runtime.triggers == []


# ---- FOCUS_CAP -------------------------------------------------------------------------------------------------------------

async def test_focus_cap_applies_outside_focus_only():
    r = Rig(focus="discord")
    e = await r.send(70, workspace="rimworld", kind="rimworld.event")
    assert e.priority == 60
    assert any("вне фокуса" in x.reason for x in r.log.for_event(e.id))
    inside = await r.send(65, workspace="discord", kind="discord.message")        # discord.message потолок 70: 65 остаётся
    assert inside.priority == 65


async def test_focus_cap_spares_user_and_critical():
    r = Rig(focus="discord")
    u = await r.send(USER, workspace="test", kind="test.ev")
    assert u.priority == USER
    crit = await r.send(70, workspace="rimworld", kind="rimworld.alert")
    assert crit.priority == 70
    core = await r.send(70, workspace="core", kind="core.timer")                   # служебное окружение ядра не режется
    assert core.priority == 70


async def test_no_focus_getter_means_no_cap():
    r = Rig()
    r.attention.focus_getter = None
    e = await r.send(90)
    assert e.priority == 90


# ---- USER-ход -------------------------------------------------------------------------------------------------------------------

async def test_user_turn_steers_same_group_and_queues_other_without_interrupt():
    r = Rig()
    r.runtime.set_batch([r.ev(USER, group_key="d:chan:u1")])
    same = await r.send(USER, group_key="d:chan:u1", text="продолжаю")
    assert r.runtime.steers == [same] and r.runtime.interrupts == []
    assert same.state is EventState.DELIVERED
    other = await r.send(USER, group_key="d:chan:u2")
    assert r.runtime.interrupts == [] and other in r.attention.queue and other.state is EventState.QUEUED
    assert r.attention.status()["steered"] == 1


async def test_user_turn_steer_unavailable_goes_to_queue():
    r = Rig()
    r.runtime.can_steer = False
    r.runtime.set_batch([r.ev(USER, group_key="g")])
    e = await r.send(USER, group_key="g")
    assert r.runtime.steers == [] and e in r.attention.queue
    r = Rig()
    r.runtime.steer_result = False
    r.runtime.set_batch([r.ev(USER, group_key="g")])
    e2 = await r.send(USER, group_key="g")                  # steer вернул False: тоже в очередь
    assert r.runtime.steers == [e2] and e2 in r.attention.queue


async def test_user_event_in_non_user_turn_interrupts_and_status_counts_reason():
    r = Rig()
    r.runtime.set_batch([r.ev(30)])
    await r.send(USER)
    assert r.attention.status()["interrupts"]["by_reason"] == {"user": 1}
    await r.clock.advance(0)
    assert "USER" in r.attention.status()["latency_by_level"]


async def test_status_basic_metrics():
    r = Rig()
    r.attention.queue.push(r.ev(50))
    await r.send(45)
    st = r.attention.status()
    assert st["queue"] == {"size": 2, "max_priority": 50, "oldest_wait_s": 0.0}
    assert st["launches"] == 0
