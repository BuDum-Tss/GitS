"""Режимы сервера, канала и ветки: ignore / read / reply, наследование, закрытая отправка на всех путях."""
import json

import pytest
from dc_rig import OWNER_ID, Channel, Msg, Rig, User

from gits_sdk import ToolError

MASHA = User(5, "masha", "Маша")


def build(rig):
    """Сервер 1 с каналом 100 «общий», веткой 300 в нём и вторым каналом 200 «тихий»."""
    g, (general, quiet) = rig.server(1, "Клуб", (("общий", 100), ("тихий", 200)))
    thread = rig.dc.register(g.add(Channel(300, "ветка", "public_thread", parent=general)))
    return g, general, quiet, thread


async def test_ignore_means_no_events_and_no_inbox_at_every_level(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, thread = build(rig)
        rig.bot.modes.set("guild", 1, "ignore")
        for i, ch in enumerate((general, quiet, thread)):
            await rig.say(Msg(i, "тихо", MASHA, ch, mentions=[rig.me]))
        assert rig.events() == [] and not (rig.mem / "discord_inbox.json").exists()
        assert not (rig.mem / "discord_known.json").exists() and not (rig.mem / "discord_dialogs.json").exists()
        # канал выше сервера, ветка выше канала
        rig.bot.modes.set("channel", 100, "reply")
        await rig.say(Msg(10, "в общем", MASHA, general))
        await rig.say(Msg(11, "в ветке общего", MASHA, thread))
        await rig.say(Msg(12, "в тихом", MASHA, quiet))
        assert [e["data"]["channel_id"] for e in rig.events()] == [100, 300]       # у ветки id канала события свой
        rig.bot.modes.set("thread", 300, "ignore")
        await rig.say(Msg(13, "ветка закрыта", MASHA, thread))
        assert len(rig.events()) == 2


async def test_read_mode_events_without_address_reply_channel_and_with_note(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, thread = build(rig)
        rig.bot.modes.set("channel", 100, "read")
        await rig.say(Msg(1, "ГЛаДОС, ответь", MASHA, general, mentions=[rig.me]))
        [ev] = rig.events()
        assert ev["kind"] == "discord.message" and ev["addressed_to_me"] is False          # режим сильнее R6
        assert ev.get("reply_channel") is None and ev["text"].endswith("[чат только для чтения: писать сюда нельзя]")
        assert ev["data"]["chat_mode"] == "read"
        # ветка наследует режим канала; своя запись ветки перекрывает
        await rig.say(Msg(2, "в ветке", MASHA, thread, mentions=[rig.me]))
        assert rig.events()[-1]["addressed_to_me"] is False
        rig.bot.modes.set("thread", 300, "reply")
        await rig.say(Msg(3, "в ветке снова", MASHA, thread, mentions=[rig.me]))
        assert rig.events()[-1]["addressed_to_me"] is True and rig.events()[-1]["reply_channel"]["target"] == "channel:300"
        assert (await rig.bot.read(f"channel:{general.id}"))                                # читать можно


async def test_read_mode_does_not_start_a_dialog(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "read"}) as rig:
        g, general, *_ = build(rig)
        await rig.say(Msg(1, "ГЛаДОС", MASHA, general))
        assert rig.bot.dialogs.in_dialog("channel:100", 5, 900) is False
        assert rig.events()[0]["addressed_to_me"] is False


async def test_default_chat_mode_applies_to_new_places_and_dm_but_owner_dm_is_always_reply(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "ignore", "owner": str(OWNER_ID)}) as rig:
        g, general, *_ = build(rig)
        owner = User(OWNER_ID, "anya", "Аня")
        await rig.say(Msg(1, "новый канал", MASHA, general, mentions=[rig.me]))
        await rig.say(Msg(2, "личка чужого", MASHA, Channel(9001, "dm", "dm", None, recipient=MASHA)))
        assert rig.events() == []
        await rig.say(Msg(3, "личка владельца", owner, Channel(9002, "dm", "dm", None, recipient=owner)))
        [ev] = rig.events()
        assert ev["kind"] == "discord.dm" and ev["addressed_to_me"] is True and ev["reply_channel"]["target"] == f"dm:{OWNER_ID}"
        rig.bot.modes.set("dm", OWNER_ID, "ignore")                                    # даже явная запись не закрывает владельца
        assert rig.bot.mode_of_dm(OWNER_ID, ["anya"]) == "reply"


async def test_effective_mode_chain_thread_channel_guild_default(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "read"}) as rig:
        g, general, quiet, thread = build(rig)
        modes, d = rig.bot.modes, rig.bot.default_mode()
        assert d == "read" and modes.get(1, 100, 300, d) == "read"
        modes.set("guild", 1, "reply")
        assert modes.get(1, 100, 300, d) == "reply" and rig.bot.mode_of_channel(thread) == "reply"
        modes.set("channel", 100, "ignore")
        assert modes.get(1, 100, 300, d) == "ignore" and modes.get(1, 200, None, d) == "reply"
        modes.set("thread", 300, "read")
        assert modes.get(1, 100, 300, d) == "read" and rig.bot.mode_of_channel(thread) == "read"
        modes.set("thread", 300, None)                                                  # «Как в канале»
        assert modes.explicit("thread", 300) is None and rig.bot.mode_of_channel(thread) == "ignore"
        with pytest.raises(ValueError):
            modes.set("channel", 100, "write")


async def test_modes_file_has_only_ids_and_modes_and_is_written_atomically(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.bot.modes.set("guild", 1, "read")
        rig.bot.modes.set("channel", 100, "ignore")
        rig.bot.modes.set("thread", 300, "reply")
        data = json.loads((rig.mem / "discord_chat_modes.json").read_text(encoding="utf-8"))
        assert data["guild"] == {"1": "read"} and data["channel"] == {"100": "ignore"} and data["thread"] == {"300": "reply"}
        assert not list(rig.mem.glob("*.tmp"))
        (rig.mem / "discord_chat_modes.json").write_text("{битый json", encoding="utf-8")        # битый файл: режимы по умолчанию
        assert rig.bot.modes.get(1, 100, 300) == "reply"


async def test_sending_is_closed_on_every_path_in_read_and_ignore(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, thread = build(rig)
        rig.bot.known.add("channel:100", "общий")
        rig.bot.modes.set("channel", 100, "read")
        rig.bot.modes.set("channel", 200, "ignore")
        for to in ("channel:100", "общий", "Клуб / общий", "channel:300"):                 # и ветка читающего канала
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool("message", {"to": to, "text": "привет"})
            assert e.value.code == "denied" and "только для чтения" in str(e.value), to
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "channel:200", "text": "привет"})
        assert "игнор" in str(e.value)
        for target in ("channel:100", "channel:200", "channel:300"):
            with pytest.raises(ToolError) as e:
                await rig.core.request("deliver", {"target": target, "text": "ответ"})
            assert e.value.code == "denied"
        assert general.sent == quiet.sent == thread.sent == []
        with pytest.raises(ToolError):
            await rig.core.call_tool("inbox", {"channel": "channel:200"})                  # игнор не читается
        # ручная отправка владельцем из панели режимом не ограничена
        assert (await rig.core.panel_action("send_message", {"target": "channel:100", "value": "от владельца"}))["ok"] is True
        assert (await rig.core.panel_action("send_message", {"target": "channel:200", "value": "и в игноре"}))["ok"] is True
        assert [m["content"] for m in general.sent + quiet.sent] == ["от владельца", "и в игноре"]


async def test_read_mode_dm_blocks_replies_but_not_the_owner(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        masha = rig.dc.user(5, "masha", display="Маша")
        owner = rig.dc.user(OWNER_ID, "anya", display="Аня")
        rig.bot.modes.set("dm", 5, "read")
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": "dm:5", "text": "привет"})
        assert e.value.code == "denied" and masha.dm_channel is None                       # даже личный канал не открывался
        await rig.core.request("deliver", {"target": f"dm:{OWNER_ID}", "text": "вопрос"})
        assert owner.dm_channel.sent[0]["content"] == "вопрос"
        await rig.core.request("deliver", {"target": "owner", "text": "ещё"})
        assert len(owner.dm_channel.sent) == 2


async def test_read_mode_tools_do_not_flip_modes_after_forbidden(tmp_path, monkeypatch):
    """Автоматических переключений режима нет: запрет Discord это только ошибка."""
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        general.deny = True
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": "channel:100", "text": "ответ"})
        assert e.value.code == "denied" and "нет прав" in str(e.value)
        assert rig.bot.modes.all() == {"guild": {}, "channel": {}, "thread": {}, "dm": {}}
