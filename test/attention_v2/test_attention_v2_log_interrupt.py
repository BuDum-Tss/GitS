"""EventLog (критерий 10) и возврат прерванных событий в очередь (критерий 4 на уровне `requeue_interrupted`)."""
from services.attention_v2.event import USER, EventState
from services.attention_v2.eventlog import EventLog
from services.attention_v2.runtime import ActionRecord
from services.attention_v2.testing import FakeClock, Rig


# ---- 10. журнал -------------------------------------------------------------------------------------------------------------

async def test_every_event_has_new_record_and_every_transition():
    r = Rig()
    queued = await r.send(10, text="ждёт")                      # new -> queued
    woke = await r.send(60, text="будит")                       # new -> queued -> delivered (в пачке с первым)
    assert r.log.states_of(woke.id) == ["new", "queued", "delivered"]
    assert r.log.states_of(queued.id) == ["new", "queued", "delivered"]

    exp = await r.send(80, expires_at=r.clock.time() - 1)
    assert r.log.states_of(exp.id) == ["new", "expired"]

    r2 = Rig(queue_max=1)
    a = await r2.send(10)
    await r2.send(20)
    assert r2.log.states_of(a.id) == ["new", "queued", "evicted"]

    r3 = Rig()
    r3.runtime.set_batch([r3.ev(30)])
    r3.runtime.had_effects = False
    first = r3.runtime.running[0]
    await r3.send(USER)
    assert r3.log.states_of(first.id) == ["interrupted", "queued"]   # из прерванного хода назад в очередь


async def test_log_entries_reference_id_and_have_seq_and_sink():
    sink = []
    clock = FakeClock()
    log = EventLog(clock=clock, sink=sink.append)
    r = Rig()
    r.log = log
    r.attention.log = log
    e = await r.send(10, text="привет")
    assert [x["event_id"] for x in sink] == [e.id, e.id]
    assert [x["op"] for x in sink] == ["new", "state"] and sink[0]["text"] == "привет"
    assert sink[0]["seq"] < sink[1]["seq"]


def test_sink_failure_does_not_break_log():
    def bad(_):
        raise RuntimeError("sink")
    log = EventLog(sink=bad)
    r = Rig()
    e = r.ev(10)
    log.record_new(e)
    assert len(log) == 1


def test_log_data_field_not_written():
    log = EventLog()
    r = Rig()
    e = r.ev(10, data={"secret": "приватное"})
    entry = log.record_new(e)
    assert "приватное" not in str(entry.to_dict())


async def test_query_filters_and_limit():
    r = Rig()
    r.focus = None
    await r.send(10, workspace="discord", kind="discord.message")
    await r.clock.advance(10)
    t1 = r.clock.time()
    await r.send(10, workspace="rimworld", kind="rimworld.event")
    await r.send(10, workspace="rimworld", kind="rimworld.alert")
    assert {x.workspace for x in r.log.query(workspace="rimworld")} == {"rimworld"}
    assert {x.kind for x in r.log.query(kind="rimworld.alert")} == {"rimworld.alert"}
    assert all(x.ts >= t1 for x in r.log.query(since=t1)) and r.log.query(since=t1)
    assert len(r.log.query(limit=2)) == 2 and r.log.query(limit=0) == []
    seqs = [x.seq for x in r.log.query(limit=3)]
    assert seqs == sorted(seqs) and seqs[-1] == len(r.log)


def test_ring_limit():
    log = EventLog(max_entries=5)
    r = Rig()
    for i in range(20):
        log.record_new(r.ev(10, text=str(i)))
    assert len(log) == 5
    assert [x.text for x in log.query()] == ["15", "16", "17", "18", "19"]


# ---- 4. прерывание ------------------------------------------------------------------------------------------------------------

async def test_interrupt_without_effects_returns_events_with_flag_and_original_priority_and_expiry():
    r = Rig()
    a = await r.send(40, text="a")                                # запустило ход пачкой [a]
    assert r.runtime.running == [a]
    exp = a.expires_at
    r.runtime.had_effects = False
    await r.send(USER, text="срочно")
    assert len(r.runtime.interrupts) == 1
    assert a in r.attention.queue and a.interrupted and a.state is EventState.QUEUED
    assert a.priority == 40 and a.expires_at == exp
    assert r.runtime.notes == [""]                                # блока «прервано» нет
    # возвращённое событие снова попадает в пачку по приоритету и помечено ⟲
    await r.runtime.finish_turn()
    assert a in r.runtime.last_trigger
    assert r.attention.render_batch(r.runtime.last_trigger).splitlines()[-1].startswith("⟲ ")


async def test_interrupt_with_completed_say_does_not_return_and_note_has_action():
    r = Rig()
    a = await r.send(40, text="спрашивают")
    r.runtime.had_effects = True
    r.runtime.actions = [ActionRecord("discord.say", "done", True, "Секунду…"), ActionRecord("memory.search", "started", False)]
    await r.send(USER, text="срочно")
    assert a not in r.attention.queue and a.interrupted and a.state is EventState.INTERRUPTED
    note = r.runtime.notes[0]
    assert "успели: discord.say «Секунду…»" in note and "не успели: memory.search" in note and "спрашивают" in note
    # блок попадает в начало следующей пачки вместе со сводкой
    text = r.attention.render_batch(r.runtime.last_trigger, note)
    assert text.splitlines()[0].startswith("(прервано:")
    assert len(r.attention.queue) == 0


async def test_requeue_interrupted_expired_event_goes_to_digest_instead():
    r = Rig()
    a = await r.send(40, expires_at=r.clock.time() + 5)
    await r.clock.advance(10)
    back = r.attention.requeue_interrupted([a], had_effects=False)
    assert back == [] and a.state is EventState.EXPIRED and r.attention.digest.total == 1


async def test_requeue_counts_in_status():
    r = Rig()
    a = await r.send(40)
    back = r.attention.requeue_interrupted([a], had_effects=False)
    assert back == [a] and r.attention.status()["requeued"] == 1
