"""Виды событий для внимания v2 (event_kinds), дорожка text и её алиас message, подсказка ответа в событии."""

from dc_rig import Rig, User, Msg

from manifest import MANIFEST, build_manifest
from services.attention_v2.event import IMPORTANT, NORMAL, USER
from services.attention_v2.kinds import KindRegistry, KindSpec, from_legacy
from test_dc_events import setup_guild


def test_event_kinds_cover_emits_and_parse():
    kinds = {k["kind"]: k for k in build_manifest()["event_kinds"]}
    assert set(kinds) == {e["kind"] for e in MANIFEST["emits"]}
    specs = {k: KindSpec.from_dict(d) for k, d in kinds.items()}
    dm, msg = specs["discord.dm"], specs["discord.message"]
    assert (dm.default_priority, dm.max_priority, dm.ttl_s, dm.reply_tool) == (IMPORTANT, USER, 900.0, "discord.text")
    assert (msg.default_priority, msg.max_priority, msg.ttl_s, msg.coalesce, msg.reply_tool) == (NORMAL, USER, 900.0, True, "discord.text")
    reg = KindRegistry()
    assert sorted(reg.register_manifest("discord", build_manifest())) == ["discord.dm", "discord.message"]


def test_text_is_the_same_tool_as_message():
    t = {x["name"]: x for x in MANIFEST["tools"]}
    assert "Основной способ" in t["text"]["description"] and t["message"]["description"].startswith("То же, что text")
    for key in ("params", "risk", "danger_check", "sends", "informative", "timeout_s"):
        assert t["text"].get(key) == t["message"].get(key)
    assert t["text"]["sends"] is True and "say" not in t                    # голосовую дорожку say даёт ядро


async def test_text_and_message_send_identically_and_events_hint_the_reply_tool(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, ch = setup_guild(rig)
        rig.bot.known.add("channel:100", "общий", "Клуб / общий")
        rig.bot.typing_scale = 0.01
        a = await rig.core.call_tool("text", {"to": "#общий", "text": "раз"})
        b = await rig.core.call_tool("message", {"to": "#общий", "text": "раз"})
        assert a == b == "отправлено в «#общий»" and [s["content"] for s in ch.sent] == ["раз", "раз"]
        masha = User(5, "masha", "Маша")
        await rig.say(Msg(1, "болтаем", masha, ch))
        await rig.say(Msg(2, "эй", masha, ch, mentions=[rig.me]))
        chatter, addressed = rig.events()
    assert chatter["reply_channel"]["tool"] == addressed["reply_channel"]["tool"] == "discord.text"
    assert chatter["addressed_to_me"] is False and chatter["urgency_hint"] == 1 and addressed["addressed_to_me"] is True
    reg = KindRegistry()
    reg.register_manifest("discord", build_manifest())
    assert from_legacy("discord", chatter, reg).prio < NORMAL and from_legacy("discord", addressed, reg).prio == USER
