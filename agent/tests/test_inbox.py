"""Входящая очередь (app/inbox.py): события срочности ниже 4 копятся и уходят размышлению одной пачкой."""
import pytest
from test_attention import Rig
from test_impulses import Spy


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.spy = Spy()
    r.att.attach_reflection(r.spy)
    return r


async def fill(rig, n=1, urgency=2):
    for i in range(n):
        await rig.submit("echo", kind=f"echo.k{i}", urgency_hint=urgency, addressed_to_me=False, text=f"Событие {i}")


async def wake(rig, advance=120.0):
    rig.clock.t += advance
    rig.att.inbox.tick(rig.clock.t)
    await rig.att.settle()


async def test_notable_events_go_to_reflection_as_one_batch(rig):
    await fill(rig, 3)
    await wake(rig)
    assert len(rig.spy.events) == 1
    ev = rig.spy.events[0]
    assert ev.kind == "inbox.batch" and not ev.trusted and "Событие 0" in ev.text and "Событие 2" in ev.text
    assert rig.att.digest_counts() == {}                                        # пачка забрана из очереди


async def test_trivia_alone_does_not_wake_the_model_until_there_is_a_lot_of_it(rig):
    await fill(rig, 3, urgency=1)
    await wake(rig)
    assert rig.spy.events == []
    await fill(rig, 8, urgency=1)
    await wake(rig)
    assert len(rig.spy.events) == 1


async def test_pause_between_batches_conversation_dnd_and_away_hold_the_batch_back(rig):
    await fill(rig, 1)
    await wake(rig)
    assert len(rig.spy.events) == 1
    await fill(rig, 1)
    await wake(rig, advance=10)                                                 # прошло меньше минуты
    assert len(rig.spy.events) == 1
    rig.cfg["dnd"] = True
    await wake(rig)
    assert len(rig.spy.events) == 1                                             # «не отвлекать»
    rig.cfg["dnd"] = False
    rig.idle = 10_000.0
    await wake(rig)
    assert len(rig.spy.events) == 1                                             # пользователь отошёл
    rig.idle = 0.0
    rig.att.focus_.touch_conversation("привет")
    await wake(rig)
    assert len(rig.spy.events) == 1                                             # идёт разговор
    rig.clock.t += 120
    rig.att.focus_.tick(rig.clock.t)
    await wake(rig)
    assert len(rig.spy.events) == 2                                             # всё закончилось: пачка ушла


async def test_nothing_without_attached_reflection(tmp_path):
    rig = Rig(tmp_path)
    await fill(rig, 3)
    await wake(rig)
    assert rig.att.digest_counts() == {"echo": 3}
