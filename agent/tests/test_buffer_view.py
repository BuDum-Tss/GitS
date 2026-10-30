"""Буфер событий для вкладки «Состояние»: судьбы событий, выдача в контекст, причина ожидания пачки, блок событий в контексте ответа."""
import pytest
from test_attention import Rig
from test_impulses import Spy

from app.context_builder import ContextBuilder


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.spy = Spy()
    r.att.attach_reflection(r.spy)
    return r


def states(rig):
    return {h["text"]: h["state"] for h in rig.att.digest_.history}


async def test_every_event_gets_a_fate_in_the_history(rig):
    await rig.submit("echo", kind="echo.a", urgency_hint=1, addressed_to_me=False, text="фон")
    await rig.submit("echo", kind="echo.b", urgency_hint=0, addressed_to_me=False, text="пустяк")
    await rig.submit("echo", kind="echo.c", urgency_hint=5, addressed_to_me=False, text="тревога")
    assert states(rig) == {"фон": "queued", "пустяк": "dropped", "тревога": "said"}
    view = rig.att.buffer_view()
    assert [q["text"] for q in view["queue"]] == ["фон"] and view["queue"][0]["weight"] > 0
    assert [h["text"] for h in view["history"]] == ["тревога", "пустяк", "фон"]                  # новые первыми


async def test_events_taken_by_the_batch_are_marked_as_context_with_the_way_they_got_there(rig):
    for i in range(3):
        await rig.submit("echo", kind=f"echo.k{i}", urgency_hint=2, addressed_to_me=False, text=f"событие {i}")
    rig.clock.t += 120
    rig.att.inbox.tick(rig.clock.t)
    await rig.att.settle()
    assert set(states(rig).values()) == {"context"}
    assert {h["via"] for h in rig.att.digest_.history} == {"batch"}
    batch = rig.att.buffer_view()["batches"][0]
    assert batch["via"] == "batch" and len(batch["ids"]) == 3


async def test_context_of_a_reply_shows_waiting_events_without_taking_them(rig):
    await rig.submit("echo", kind="echo.a", urgency_hint=2, addressed_to_me=False, text="письмо от торговцев")
    text = rig.att.context_events()
    assert "письмо от торговцев" in text and "ещё ждут" in text
    assert rig.att.digest_counts() == {"echo": 1}                                                # из очереди не ушло
    assert states(rig) == {"письмо от торговцев": "context"} and rig.att.digest_.history[-1]["via"] == "reply"
    assert rig.att.context_events() != ""


async def test_evicted_events_are_marked_in_the_history(rig):
    await rig.submit("other", kind="other.rare", urgency_hint=2, addressed_to_me=False, text="редкое")
    for i in range(120):
        await rig.submit("echo", kind="echo.tick", urgency_hint=1, addressed_to_me=False, text=f"тик {i}")
    evicted = [h for h in rig.att.digest_.history if h["state"] == "evicted"]
    assert len(evicted) == 21 and all(h["module"] == "echo" for h in evicted)                    # ушла рутина, не редкое
    assert states(rig)["редкое"] == "queued"


async def test_blocker_says_why_the_batch_waits(rig):
    now = rig.clock.t + 1000
    assert rig.att.inbox.blocker(now) == "очередь пуста"
    await rig.submit("echo", kind="echo.a", urgency_hint=1, addressed_to_me=False, text="мелочь")
    assert "мелочь копится" in rig.att.inbox.blocker(now)
    await rig.submit("echo", kind="echo.b", urgency_hint=2, addressed_to_me=False, text="заметное")
    assert rig.att.inbox.blocker(now) == ""
    rig.att.inbox.last_wake = now - 10
    assert rig.att.inbox.blocker(now).startswith("пауза между пачками")
    rig.att.inbox.last_wake = 0.0
    rig.cfg["dnd"] = True
    assert rig.att.inbox.blocker(now) == "«не отвлекать»"
    assert rig.att.buffer_view()["inbox"]["ready"] is False


def test_context_builder_puts_the_events_block_into_the_context():
    class Host:
        events_block = staticmethod(lambda: "События, которые накопились и ещё ждут:\n- Демо: что-то случилось")

    assert "Демо: что-то случилось" in ContextBuilder(Host()).events_block()
    assert ContextBuilder(object()).events_block() == ""                                         # без источника блока нет

    class Broken:
        @staticmethod
        def events_block():
            raise RuntimeError("сбой")

    assert ContextBuilder(Broken()).events_block() == ""                                         # сбой источника контекст не ломает
