"""Журнал входа агента (`event_trace`): кольцо, укорочение текста и запись из `ResidentRuntime` на поддельной среде."""
import pytest

from http_api import presenters
from services.attention_v2.event import USER
from services.tracing import event_trace

from test_resident_runtime import Rig, wait_until


@pytest.fixture(autouse=True)
def _clean():
    event_trace.clear()
    yield
    event_trace.clear()


def test_ring_is_bounded_and_text_is_clipped():
    event_trace.add("discord", "message", "x" * 1000, 50)
    assert len(event_trace.recent(1)[0]["text"]) == event_trace.TEXT_MAX
    for i in range(event_trace.RING + 7):
        event_trace.add("core", "tick", f"t{i}")
    got = event_trace.recent(1000)
    assert len(got) == event_trace.RING and got[-1]["text"] == f"t{event_trace.RING + 6}"
    assert [e["text"] for e in event_trace.recent(2)] == [f"t{event_trace.RING + 5}", f"t{event_trace.RING + 6}"]


def test_entry_shape_and_unknown_how_falls_back_to_batch():
    event_trace.add("rimworld", "raid", "налёт", 70, "steered", turn=3)
    event_trace.add("core", "x", "y", None, "странный")
    a, b = event_trace.recent()
    assert (a["source"], a["kind"], a["priority"], a["how"], a["turn"]) == ("rimworld", "raid", 70, "steered", 3)
    assert b["how"] == "batch" and "turn" not in b and a["id"] != b["id"] and a["ts"] > 0


async def test_batch_of_three_events_gives_three_batch_entries():
    r = Rig()
    events = [r.ev(40, text=f"событие {i}") for i in range(3)]
    await r.runtime.trigger(events)
    await wait_until(lambda: r.model.prompts)
    got = [e for e in event_trace.recent() if e["how"] == "batch"]
    assert len(got) == 3 and {e["text"] for e in got} == {"событие 0", "событие 1", "событие 2"}
    assert all(e["source"] == "test" and e["kind"] == "ev" and e["turn"] == 1 for e in got)
    await r.idle()
    await r.close()


async def test_steer_writes_steered_entry():
    r = Rig([[("hold",)], []])
    await r.send(USER, text="привет", group_key="chan:u1")
    await r.clock.advance(1)
    await wait_until(lambda: r.model.prompts)
    await r.send(USER, text="и ещё вот что", group_key="chan:u1")
    steered = [e for e in event_trace.recent() if e["how"] == "steered"]
    assert len(steered) == 1 and steered[0]["text"] == "и ещё вот что" and steered[0]["priority"] == USER
    r.model.gate.set()
    await r.idle()
    await r.close()


async def test_interrupt_note_is_written():
    r = Rig([[("tool", "test.send", {"text": "Секунду"}), ("hold",)], []])
    await r.send(40, text="старое")
    await wait_until(lambda: any(a.status == "done" for a in r.agent.turn_actions))
    await r.send(90, text="срочное")
    await wait_until(lambda: len(r.model.prompts) == 2)
    notes = [e for e in event_trace.recent() if e["how"] == "interrupt_note"]
    assert len(notes) == 1 and "прервано" in notes[0]["text"]
    assert any(e["how"] == "batch" and e["text"] == "срочное" for e in event_trace.recent())
    await r.idle()
    await r.close()


def test_digest_line_is_written_as_digest_entry():
    r = Rig()
    from services.runtime.turns import _Turn
    e = r.ev(40, text="одно")
    turn = _Turn(events=[e], no=5)
    r.runtime._trace_batch(turn, "(пропущено: 3 события)\n" + "[00:00:00] test/ev одно", "")
    kinds = [(x["how"], x["text"]) for x in event_trace.recent()]
    assert kinds[0] == ("digest", "(пропущено: 3 события)") and kinds[1] == ("batch", "одно")


def test_core_payload_carries_events_in_and_defaults_to_empty():
    base = dict(dialogue=[], activity=[], screen=None, memories=[], relations=[], approval={})
    assert presenters.core_payload(**base)["events_in"] == []
    assert presenters.core_payload(**base, events_in=[{"id": 1}])["events_in"] == [{"id": 1}]
