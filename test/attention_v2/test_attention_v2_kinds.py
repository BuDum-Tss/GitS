"""Виды событий и перевод старых событий шины: критерий приёмки 8 (потолок, неизвестный вид), `from_legacy`."""
import pytest

from services.attention_v2.event import BACKGROUND, IMPORTANT, NORMAL, NOTABLE, USER, Event, EventState
from services.attention_v2.kinds import KindRegistry, KindSpec, default_ttl, from_legacy
from services.attention_v2.testing import Rig, default_registry


# ---- реестр и normalize -----------------------------------------------------------------------------------------------

def test_register_manifest_names_and_numbers():
    reg = KindRegistry()
    kinds = reg.register_manifest("x", {"event_kinds": [
        {"kind": "x.a", "default_priority": "IMPORTANT", "max_priority": 80, "ttl_s": 30, "coalesce": True, "reply_tool": "x.text"},
        {"kind": "x.b"}]})
    assert kinds == ["x.a", "x.b"] and reg.kinds("x") == ["x.a", "x.b"]
    a = reg.get("x.a")
    assert (a.default_priority, a.max_priority, a.ttl_s, a.coalesce, a.reply_tool, a.critical) == (IMPORTANT, 80, 30, True, "x.text", False)
    b = reg.get("x.b")
    assert (b.default_priority, b.max_priority) == (NORMAL, USER)
    reg.register_manifest("x", [{"kind": "x.c"}])               # повторное объявление заменяет прежнее
    assert reg.kinds("x") == ["x.c"] and "x.a" not in reg


def test_manifest_validation():
    reg = KindRegistry()
    with pytest.raises(ValueError):
        reg.register_manifest("x", [{"kind": "x.a", "max_priority": "HUGE"}])
    with pytest.raises(ValueError):
        reg.register_manifest("x", [{"kind": "x.a", "ttl_s": -1}])
    with pytest.raises(ValueError):
        reg.register_manifest("x", [{"ttl_s": 5}])


def test_normalize_clamps_defaults_and_sets_expiry():
    reg = default_registry()
    e = Event(workspace="discord", kind="discord.message", text="т", priority=100, ts=500.0)
    res = reg.normalize(e)
    assert res.accepted and res.clamped_from == 100 and e.priority == IMPORTANT
    assert e.expires_at == 500.0 + 900 and e.reply.tool == "discord.text"
    kept = Event(workspace="discord", kind="discord.message", text="т", ts=500.0, expires_at=510.0)
    reg.normalize(kept)
    assert kept.priority == NORMAL and kept.expires_at == 510.0          # свой срок не затирается, приоритет по умолчанию


def test_normalize_truncates_long_text():
    reg = default_registry()
    e = Event(workspace="discord", kind="discord.message", text="я" * 5000)
    reg.normalize(e)
    assert len(e.text) == 1000 and e.text.endswith("…")


def test_default_ttl_by_kind():
    assert default_ttl("discord.voice.utterance") == 90
    assert default_ttl("telegram.dm") == 900
    assert default_ttl("rimworld.raid") == 60
    assert default_ttl("core.timer") == 600
    assert default_ttl("discord.spoken") == 120


async def test_ceiling_is_clamped_and_logged():
    r = Rig(focus="discord")
    e = await r.send(100, workspace="discord", kind="discord.message")
    assert e.priority == IMPORTANT
    notes = [x for x in r.log.for_event(e.id) if x.op == "note"]
    assert notes and notes[0].extra["before"] == 100 and notes[0].extra["after"] == IMPORTANT


async def test_unknown_kind_dropped_with_reason_in_log():
    r = Rig()
    e = await r.send(USER, workspace="ghost", kind="ghost.boo")
    assert e.state is EventState.DROPPED
    assert r.runtime.triggers == [] and len(r.attention.queue) == 0
    last = r.log.for_event(e.id)[-1]
    assert last.state == "dropped" and "ghost.boo" in last.reason
    assert r.log.states_of(e.id) == ["new", "dropped"]
    assert r.attention.status()["dropped"] == 1


async def test_spoken_result_is_background_event_and_does_not_wake():
    r = Rig()
    e = await r.send(None, workspace="discord", kind="discord.spoken", text="прозвучало: привет")
    assert e.priority == BACKGROUND and e.expires_at == e.ts + 120
    assert r.runtime.triggers == []


# ---- from_legacy -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hint,expected", [(0, 0), (1, 10), (2, 30), (3, 50), (4, 70), (5, 100)])
def test_legacy_urgency_mapping(hint, expected):
    reg = KindRegistry()
    e = from_legacy("telegram", {"kind": "telegram.dm", "text": "привет", "urgency_hint": hint}, reg, now=100.0)
    assert e.priority == expected and e.workspace == "telegram" and e.ts == 100.0


def test_legacy_out_of_range_hint_is_clamped_and_missing_uses_default():
    reg = KindRegistry()
    assert from_legacy("t", {"kind": "t.a", "text": "x", "urgency_hint": 9}, reg).priority == 100
    assert from_legacy("t", {"kind": "t.a", "text": "x", "urgency_hint": -3}, reg).priority == 0
    e = from_legacy("t", {"kind": "t.a", "text": "x"}, reg)
    assert e.priority is None
    assert reg.normalize(e).accepted and e.priority == NORMAL


def test_legacy_addressed_to_me_raises_to_user_unless_bot():
    reg = KindRegistry()
    d = {"kind": "telegram.dm", "text": "эй", "urgency_hint": 1, "addressed_to_me": True,
         "sender": {"id": "1", "name": "Маша", "is_owner": False}}
    assert from_legacy("telegram", d, reg).priority == USER
    bot = dict(d, sender={"id": "2", "name": "Бот", "is_bot": True})
    assert from_legacy("telegram", bot, reg).priority == 10                 # бот не поднимает
    assert from_legacy("telegram", dict(d, addressed_to_me=False), reg).priority == 10
    assert from_legacy("telegram", dict(d, sender=None), reg).priority == USER


def test_legacy_ttl_expires_in_s_and_default():
    reg = KindRegistry()
    e = from_legacy("telegram", {"kind": "telegram.dm", "text": "x", "expires_in_s": 30}, reg, now=1000.0)
    assert e.expires_at == 1030.0
    d = from_legacy("telegram", {"kind": "telegram.dm", "text": "x"}, reg, now=1000.0)
    assert d.expires_at is None
    assert reg.normalize(d).accepted and d.expires_at == 1000.0 + 900         # по умолчанию (чат, раздел 3.3)


def test_legacy_undeclared_kind_accepted_with_default_spec():
    reg = KindRegistry()
    e = from_legacy("rimworld", {"kind": "rimworld.raid", "text": "налёт", "urgency_hint": 4}, reg, now=0.0)
    res = reg.normalize(e)
    assert res.accepted and res.spec.legacy and e.priority == IMPORTANT and e.expires_at == 60.0
    assert "rimworld.raid" in reg


def test_legacy_declared_kind_keeps_manifest_rules():
    reg = KindRegistry()
    reg.register(KindSpec(kind="telegram.dm", default_priority=NOTABLE, max_priority=NOTABLE, ttl_s=5))
    e = from_legacy("telegram", {"kind": "telegram.dm", "text": "x", "urgency_hint": 5}, reg, now=10.0)
    reg.normalize(e)
    assert e.priority == NOTABLE and e.expires_at == 15.0


def test_legacy_fields_converted():
    reg = KindRegistry()
    e = from_legacy("telegram", {"kind": "telegram.message", "text": "привет", "sender": {"id": "7", "name": "Вася", "is_owner": True},
                                 "reply_channel": {"module": "telegram", "target": "42"}, "dedupe_key": "tg:42",
                                 "data": {"chat": 42}}, reg, now=5.0, event_id="fixed")
    assert e.id == "fixed" and e.group_key == "tg:42" and e.data == {"chat": 42}
    assert e.sender.name == "Вася" and e.sender.is_owner and not e.sender.is_bot
    assert e.reply.tool == "telegram.message" and e.reply.target == "42"


async def test_legacy_event_flows_through_attention():
    r = Rig()
    e = from_legacy("telegram", {"kind": "telegram.dm", "text": "срочно", "urgency_hint": 4}, r.registry, now=r.clock.time())
    r.focus = "telegram"
    await r.attention.process_event(e)
    assert len(r.runtime.triggers) == 1 and r.runtime.last_trigger == [e]


def test_from_legacy_takes_reply_tool_and_group_key_from_the_bus_event():
    """Окружения шины: инструмент ответа берётся из `reply_channel.tool`, ключ слияния из `data.group_key`."""
    ev = from_legacy("discord", {"kind": "discord.message", "text": "привет", "addressed_to_me": False,
                                 "reply_channel": {"module": "discord", "target": "channel:1", "tool": "discord.text"},
                                 "data": {"group_key": "discord:1:5"}, "dedupe_key": "discord:message:1:77"}, KindRegistry())
    assert ev.reply.tool == "discord.text" and ev.reply.target == "channel:1"
    assert ev.group_key == "discord:1:5"


def test_from_legacy_reply_tool_falls_back_to_message():
    ev = from_legacy("telegram", {"kind": "telegram.dm", "text": "x", "reply_channel": {"module": "telegram", "target": "user:1"}},
                     KindRegistry())
    assert ev.reply.tool == "telegram.message"
