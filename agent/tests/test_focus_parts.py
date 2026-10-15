"""Части менеджера фокуса по отдельности (ТЗ 29, ARC-6): правила, очередь, хранилище checkpoint, контракт к модели.
Без модулей, сети и LM Studio."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import focus_dispatch as dispatch
from domain import focus_policy as pol
from focus_queue import DEFERRED, EXPIRED, PENDING, FocusQueue, EVENT_TTL_S
from focus_store import CHECKPOINT_MAX_BYTES, CheckpointStore, json_safe


def _gate(**kw):
    base = dict(critical=False, once=False, quiet=False, profile_quiet=False, quiet_policy=None, pinned=None,
                pinned_title="", module="game", busy=False, focus_needed=None, focus_conf=0.0, priority=70.0,
                has_slot=False, slot_since=0.0, switch_times=[], now=1000.0, holder=0.0, interruptible=True)
    base.update(kw)
    return pol.gate(**base)


def test_gate_rules():
    assert _gate()[0] == "switch"
    assert _gate(priority=40)[0] == "defer"
    assert _gate(critical=True, quiet=True)[0] == "switch"
    assert _gate(quiet=True)[0] == "defer"
    assert _gate(quiet=True, once=True)[0] == "switch"
    assert _gate(pinned="tg", pinned_title="Telegram")[1].endswith("«Telegram»")
    assert _gate(focus_needed=False, focus_conf=0.9)[0] == "defer"
    assert _gate(has_slot=True, slot_since=995.0, holder=60.0, priority=90)[1].startswith("дребезг")
    assert _gate(has_slot=True, slot_since=0.0, holder=60.0, priority=70)[0] == "defer"
    assert _gate(has_slot=True, slot_since=0.0, holder=60.0, priority=90, interruptible=False)[0] == "defer"
    assert _gate(has_slot=True, slot_since=0.0, holder=60.0, priority=90)[0] == "switch"


def test_ambiguity_and_recheck_and_holder():
    kw = dict(critical=False, has_llm=True, llm_used=False, confidence=None, near=False, conflict=False,
              claimant=False, hint=None)
    assert pol.ambiguity(**kw) is None
    assert pol.ambiguity(**{**kw, "confidence": 0.3}).startswith("низкая")
    assert pol.ambiguity(**{**kw, "near": True}) is not None
    assert pol.ambiguity(**{**kw, "near": True, "confidence": 0.9}) is None
    assert pol.ambiguity(**{**kw, "has_llm": False, "confidence": 0.1}) is None
    assert pol.needs_recheck("danger", 0.2, False) and not pol.needs_recheck("danger", 0.2, True)
    assert pol.holder_priority(None) == 0.0 and pol.holder_priority("user") == pol.USER_PRIORITY
    assert pol.holder_priority("event") == pol.HOLDER_PRIORITY_EVENT


def _rec(text="e", kind="game.x", priority=50.0, **kw):
    rec = dict(module="game", kind=kind, text=text, priority=priority, urgency=None, critical=False, confidence=None,
               category=None, expires=None, dedupe_key=None, hint=None, hint_conf=None, focus_needed=None,
               focus_conf=None)
    rec.update(kw)
    return rec


def _queue():
    log: list = []
    touched: list = []
    return FocusQueue(log=lambda t, **f: log.append(t), touch=lambda: touched.append(1)), log, touched


def test_queue_merges_duplicates_and_expires():
    q, log, touched = _queue()
    q.push(_rec("a"))
    q.push(_rec("b", priority=65.0))
    q.drain(100.0)
    assert len(q.events) == 1 and "merge" in log and touched
    ev = next(iter(q.events.values()))
    assert ev.count == 2 and ev.text == "b" and ev.base == 65.0
    q.defer(ev, {"type": "context_changed"}, "причина", 101.0)
    assert ev.status == DEFERRED
    q.expire(100.0 + EVENT_TTL_S + 1)
    assert ev.status == EXPIRED


def test_queue_ages_and_refreshes_and_roundtrips():
    q, _, _ = _queue()
    q.push(_rec("a", priority=40.0, kind="game.y"))
    q.drain(0.0)
    ev = next(iter(q.events.values()))
    q.age(700.0)
    assert ev.priority > 40.0
    q.defer(ev, {"type": "context_changed"}, "r", 700.0)
    q.refresh(701.0, changed=True)
    assert ev.status == PENDING
    other, _, _ = _queue()
    other.load(q.dump(), 800.0)
    assert [e.module for e in other.events.values()] == ["game"]


def test_store_checkpoint_and_file(tmp_path):
    path = tmp_path / "focus.json"
    store = CheckpointStore(path)
    slot = SimpleNamespace(module="game", task_id="t1", policy="combat", goal="g", priority=60.0, origin="event")
    cp = store.create(slot, {"a": 1}, "paused", "r", {"type": "holder_free"}, 10.0)
    assert store.dirty and cp["payload"] == {"a": 1} and cp["scratch"] == "game/scratch"
    assert store.resumable() == [cp]
    asyncio.run(store.save({"v": 1, "checkpoints": [cp]}))
    assert not store.dirty and json.loads(path.read_text(encoding="utf-8"))["v"] == 1
    again = CheckpointStore(path)
    assert again.read() is not None and "t1" in again.checkpoints
    assert "note" in json_safe({"x": "я" * CHECKPOINT_MAX_BYTES})
    assert json_safe("не словарь") is None


def test_dispatch_consult_statuses():
    async def go():
        assert await dispatch.FocusDispatch().consult({}, 0.0) is None

        async def bad(ctx):
            raise ValueError("не json")

        async def boom(ctx):
            raise RuntimeError("упало")

        async def ok(ctx):
            return {"reason": "ничего", "operations": []}

        d = dispatch.FocusDispatch(bad)
        assert (await d.consult({"events": {}}, 0.0)).status == "invalid"
        d.llm = boom
        res = await d.consult({"events": {}}, 0.0)
        assert res.status == "unavailable" and res.error == "упало"
        d.llm = ok
        res = await d.consult({"events": {}}, 0.0)
        assert res.status == "ok" and res.ops == []

        async def slow(event):
            await asyncio.sleep(1)

        d.recheck = slow
        assert await d.urgency({}, 0.01) == (None, "TimeoutError")
        assert await dispatch.FocusDispatch().urgency({}) == (None, "")

    asyncio.run(go())
