"""Важное событие в идущем ходе: доливается сразу (агент видит его на ближайшем шаге), слабые и пассивные ждут в очереди."""
from services.attention_v2.testing import Rig


async def test_important_event_is_steered_into_the_running_turn():
    r = Rig()
    r.runtime.set_batch([r.ev(70)])                        # идёт ход по событию того же уровня: прерывать нельзя (нужно > 90)
    e = await r.send(70)                                   # гибель колониста
    assert r.runtime.steers == [e] and r.runtime.interrupts == []
    assert e not in r.attention.queue and r.attention.status()["steered"] == 1


async def test_weak_event_waits_in_the_queue_during_a_turn():
    r = Rig()
    r.runtime.set_batch([r.ev(70)])
    e = await r.send(50)
    assert r.runtime.steers == [] and e in r.attention.queue


async def test_steer_failure_falls_back_to_the_queue():
    r = Rig()
    r.runtime.set_batch([r.ev(70)])
    r.runtime.steer_result = False                         # ход успел закончиться
    e = await r.send(70)
    assert e in r.attention.queue and r.attention.status()["steered"] == 0


async def test_same_kind_is_steered_not_more_often_than_the_gap():
    r = Rig()
    r.runtime.set_batch([r.ev(70)])
    first = await r.send(70)
    second = await r.send(70)                              # тот же вид сразу же: ждёт в очереди (слияние), не заваливает ход
    assert r.runtime.steers == [first] and second in r.attention.queue
    await r.clock.advance(r.attention.steer_gap_s + 1)
    third = await r.send(70)
    assert r.runtime.steers == [first, third]
