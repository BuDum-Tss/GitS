"""Виды событий для внимания v2 (event_kinds), дорожка text и её алиас message, подсказка ответа в событии."""
from test_tg_events import FakeEvent
from tg_rig import Rig

from manifest import MANIFEST, build_manifest
from services.attention_v2.event import BACKGROUND, IMPORTANT, NORMAL, NOTABLE, USER
from services.attention_v2.kinds import KindRegistry, KindSpec, from_legacy


def test_event_kinds_cover_emits_and_parse():
    kinds = {k["kind"]: k for k in build_manifest()["event_kinds"]}
    assert set(kinds) == {e["kind"] for e in MANIFEST["emits"]}
    specs = {k: KindSpec.from_dict(d) for k, d in kinds.items()}
    dm, grp, posted = specs["telegram.dm"], specs["telegram.group"], specs["telegram.posted"]
    assert (dm.default_priority, dm.max_priority, dm.ttl_s, dm.reply_tool) == (IMPORTANT, USER, 900.0, "telegram.text")
    assert (grp.default_priority, grp.max_priority, grp.coalesce, grp.reply_tool) == (NORMAL, USER, True, "telegram.text")
    assert (posted.default_priority, posted.max_priority, posted.reply_tool) == (BACKGROUND, NOTABLE, None)
    assert len(KindRegistry().register_manifest("telegram", build_manifest())) == 3


def test_text_is_the_same_tool_as_message():
    t = {x["name"]: x for x in MANIFEST["tools"]}
    assert "Основной способ" in t["text"]["description"] and t["message"]["description"].startswith("То же, что text")
    for key in ("params", "risk", "danger_check", "sends", "informative", "timeout_s"):
        assert t["text"].get(key) == t["message"].get(key)
    assert t["text"]["sends"] is True


async def test_text_and_message_send_identically_and_events_hint_the_reply_tool(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        from test_tg_message import fake_entity
        await fake_entity(rig)
        a = await rig.core.call_tool("text", {"to": "Маша", "text": "привет"})
        b = await rig.core.call_tool("message", {"to": "Маша", "text": "привет"})
        assert a == b == "отправлено в «Маша»" and rig.tl.sent == [(("chat", "Маша"), "привет")] * 2
        user = rig.mod.user
        user._me = type("M", (), {"first_name": "Глэдис", "username": "glados_acc"})()
        base = dict(private=False, group=True, title="Соседи", chat_id=-100, sender_id=9, first="Петя")
        await user._on_incoming(FakeEvent(text="болтаем", msg_id=1, **base))
        await user._on_incoming(FakeEvent(text="@glados_acc ау", msg_id=2, **base))
        await rig.core.wait_for(lambda: len(rig.core.events) == 2)
        chatter, addressed = rig.core.events
    assert chatter["reply_channel"]["tool"] == addressed["reply_channel"]["tool"] == "telegram.text"
    assert chatter["addressed_to_me"] is False and chatter["urgency_hint"] == 1 and addressed["addressed_to_me"] is True
    reg = KindRegistry()
    reg.register_manifest("telegram", build_manifest())
    assert from_legacy("telegram", chatter, reg).prio < NORMAL and from_legacy("telegram", addressed, reg).prio == USER
