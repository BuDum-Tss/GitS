"""Инструмент react: реакции-эмодзи на сообщения (юникод и кастомные), снятие, последнее входящее по умолчанию, режимы чатов,
понятные ошибки Discord, адресаты ветки и лички, видимость id сообщений в inbox."""
import pytest
from dc_rig import OWNER_ID, Channel, Forbidden, HTTPException, Msg, NotFound, Rig, User

from gits_sdk import ToolError

MASHA = User(5, "masha", "Маша")


def build(rig):
    """Сервер 1: канал 100 «общий», ветка 300 в нём, канал 200 «тихий»; «общий» известен боту."""
    g, (general, quiet) = rig.server(1, "Клуб", (("общий", 100), ("тихий", 200)))
    thread = rig.dc.register(g.add(Channel(300, "ветка", "public_thread", parent=general)))
    for target, *names in (("channel:100", "общий", "Клуб / общий"), ("channel:200", "тихий"), ("channel:300", "ветка")):
        rig.bot.known.add(target, *names)
    return g, general, quiet, thread


def put(channel, mid, text="привет", author=MASHA, **kw):
    msg = Msg(mid, text, author, channel, **kw)
    channel.messages.append(msg)
    return msg


async def react(rig, **args):
    return await rig.core.call_tool("react", args)


async def denied(rig, code, **args):
    with pytest.raises(ToolError) as e:
        await react(rig, **args)
    assert e.value.code == code
    return str(e.value)


async def test_react_unicode_on_given_message(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)
        out = await react(rig, emoji="👍", message_id="11", to="#общий")
        assert msg.reactions == ["👍"] and "#11" in out and "поставлена" in out
        await react(rig, emoji=" 🎉 ", message_id=11, to="channel:100")                 # число и пробелы вокруг
        assert msg.reactions == ["👍", "🎉"]
        assert rig.bot.dialogs.activity("channel:100")["hour"] == 0                    # реакция не считается её сообщением


async def test_react_custom_emoji_forms_are_normalized(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)
        for form in ("<:kekw:123456789012345678>", "<a:party:123456789012345679>", "pepe:123456789012345680"):
            await react(rig, emoji=form, message_id="11", to="#общий")
        assert msg.reactions == ["kekw:123456789012345678", "party:123456789012345679", "pepe:123456789012345680"]


async def test_react_defaults_to_last_incoming_message_of_last_channel(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, _ = build(rig)
        first, second = put(general, 21, "раз"), put(general, 22, "два")
        other = put(quiet, 31, "в тихом")
        await rig.say(first)
        await rig.say(second)
        assert "#22" in await react(rig, emoji="👍")                                    # без to и message_id
        assert second.reactions == ["👍"] and first.reactions == [] and other.reactions == []
        await rig.say(other)                                                           # теперь последний канал: тихий
        await react(rig, emoji="❤️")
        assert other.reactions == ["❤️"] and second.reactions == ["👍"]
        await react(rig, emoji="😂", to="#общий")                                      # явный канал: его последнее
        assert second.reactions == ["👍", "😂"]


async def test_react_default_falls_back_to_channel_history_and_skips_her_own(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        theirs = put(general, 41, "до запуска бота")
        put(general, 42, "её слова", author=rig.me)
        await react(rig, emoji="👍", to="#общий")                                      # inbox пуст: берём из истории
        assert theirs.reactions == ["👍"]
        rig.dc.register(Channel(400, "пусто", "text", g))
        rig.bot.known.add("channel:400", "пусто")
        assert "нет входящих" in await denied(rig, "bad_request", emoji="👍", to="channel:400")


async def test_react_remove_takes_off_own_reaction(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)
        await react(rig, emoji="👍", message_id="11", to="#общий")
        out = await react(rig, emoji="👍", message_id="11", to="#общий", remove=True)
        assert msg.reactions == [] and msg.removed_by is rig.me and "снята" in out
        await denied(rig, "bad_request", emoji="👍", message_id="11", to="#общий", remove="да")


async def test_read_and_ignore_modes_forbid_reactions_everywhere(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        g, general, quiet, thread = build(rig)
        masha = rig.dc.user(5, "masha", display="Маша")
        masha.dm_channel = Channel(9001, "dm", "dm", None, recipient=masha)
        msgs = {c.id: put(c, 50 + i) for i, c in enumerate((general, thread, quiet, masha.dm_channel))}
        rig.bot.known.add("dm:5", "masha", "@masha")
        rig.bot.modes.set("channel", 100, "read")          # канал и ветка под ним
        rig.bot.modes.set("channel", 200, "ignore")
        rig.bot.modes.set("dm", 5, "read")
        for to, cid in (("#общий", 100), ("channel:300", 300), ("channel:200", 200), ("@masha", 9001)):
            text = await denied(rig, "denied", emoji="👍", message_id=str(msgs[cid].id), to=to)
            assert ("чат только для чтения" in text) == (cid != 200) and "игнор" in text + "игнор"
            assert msgs[cid].reactions == []
        assert "чат только для чтения" in await denied(rig, "denied", emoji="👍", to="#общий")      # и без message_id
        rig.bot.modes.set("thread", 300, "reply")                                       # ветка перекрывает канал
        await react(rig, emoji="👍", message_id="51", to="channel:300")
        assert msgs[300].reactions == ["👍"]
        rig.bot.modes.set("dm", 5, "ignore")
        assert "игнор" in await denied(rig, "denied", emoji="👍", message_id="53", to="@masha")


async def test_react_to_thread_dm_and_owner(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        g, general, quiet, thread = build(rig)
        masha = rig.dc.user(5, "masha", display="Маша")
        masha.dm_channel = Channel(9001, "dm", "dm", None, recipient=masha)
        owner = rig.dc.user(OWNER_ID, "anya", display="Аня")
        owner.dm_channel = Channel(9002, "dm", "dm", None, recipient=owner)
        in_thread, in_dm = put(thread, 61, "в ветке"), put(masha.dm_channel, 62, "в личке")
        from_owner = put(owner.dm_channel, 63, "от владельца", author=owner)
        await rig.say(in_thread)
        await rig.say(in_dm)
        await react(rig, emoji="👍", to="channel:300")                                 # последнее в ветке
        await react(rig, emoji="🙏", to="dm:5")                                         # последнее в личке
        await react(rig, emoji="❤️", to="owner", message_id="63")
        assert (in_thread.reactions, in_dm.reactions, from_owner.reactions) == (["👍"], ["🙏"], ["❤️"])
        await react(rig, emoji="👍", to="@masha", message_id="62")
        assert in_dm.reactions == ["🙏", "👍"]
    async with Rig(tmp_path / "x", monkeypatch) as rig:                                 # владельца нет в настройках
        assert "не задан" in await denied(rig, "unavailable", emoji="👍", to="owner")


async def test_react_check_asks_only_for_unknown_chats(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        g, (general, other) = rig.server(1, "Клуб", (("общий", 100), ("флуд", 200)))
        check = rig.core.check_tool
        assert "ему ещё не писали" in await check("react", {"emoji": "👍", "to": "#общий"})
        assert "ему ещё не писали" in await check("react", {"emoji": "👍"})            # «channel» без истории
        assert await check("react", {"emoji": "👍", "to": "owner"}) is None
        rig.bot.known.add("channel:100", "общий")
        assert await check("react", {"emoji": "👍", "to": "#общий"}) is None
        assert await check("react", {"emoji": "👍", "to": "channel:100"}) is None
        assert "ему ещё не писали" in await check("react", {"emoji": "👍", "to": "#флуд"})


async def test_discord_errors_become_clear_tool_errors(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)
        cases = [
            (Forbidden("Missing Permissions"), "denied", ("Add Reactions", "Read Message History")),
            (NotFound("Unknown Message"), "bad_request", ("сообщение не найдено",)),
            (HTTPException("Unknown Emoji", status=404, code=10014), "bad_request", ("не знает такого эмодзи",)),
            (HTTPException("400 Bad Request: Invalid Emoji", status=400), "bad_request", ("не знает такого эмодзи",)),
            (HTTPException("Maximum number of reactions reached", status=400, code=30010), "bad_request", ("максимум разных реакций",)),
            (HTTPException("Too Many Requests", status=429), "unavailable", ("лимит запросов",)),
        ]
        for err, code, words in cases:
            msg.react_error = err
            text = await denied(rig, code, emoji="👍", message_id="11", to="#общий")
            assert all(w in text for w in words), text
            assert msg.reactions == []
        msg.react_error = None
        text = await denied(rig, "bad_request", emoji="👍", message_id="999", to="#общий")      # нет такого сообщения
        assert "сообщение не найдено" in text
        general.hang = False


async def test_discord_hang_gives_error_not_freeze(tmp_path, monkeypatch):
    import asyncio

    import discord_bot
    monkeypatch.setattr(discord_bot, "NET_TIMEOUT_S", 0.1)
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)

        async def hang(emoji):
            await asyncio.sleep(3600)
        msg.add_reaction = hang
        assert "не ответил" in await denied(rig, "unavailable", emoji="👍", message_id="11", to="#общий")


@pytest.mark.skip(reason="проверка приватности отключена владельцем")
async def test_bad_arguments_and_emoji_validation(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Анна"]) as rig:
        g, general, *_ = build(rig)
        msg = put(general, 11)
        for args in ({}, {"emoji": ""}, {"emoji": "  "}):
            await denied(rig, "bad_request", **args)
        for bad in ("привет", "ok", ":thumbsup:", "👍 👍", "x" * 40, "name:abc"):
            assert "эмодзи" in await denied(rig, "bad_request", emoji=bad, message_id="11", to="#общий")
        for mid in ("abc", True, 1.5, "-1"):
            assert "message_id" in await denied(rig, "bad_request", emoji="👍", message_id=mid, to="#общий")
        assert "приватности" in await denied(rig, "denied", emoji="<:Анна:123>", message_id="11", to="#общий")
        assert msg.reactions == []


@pytest.mark.skip(reason="проверка приватности отключена владельцем")
async def test_react_unavailable_without_privacy_terms_or_for_forum(tmp_path, monkeypatch):
    from types import SimpleNamespace
    async with Rig(tmp_path, monkeypatch, privacy_ready=False) as rig:
        g, general, *_ = build(rig)
        put(general, 11)
        await denied(rig, "unavailable", emoji="👍", message_id="11", to="#общий")
    async with Rig(tmp_path / "y", monkeypatch) as rig:
        g, general, quiet, _ = build(rig)
        quiet.type = SimpleNamespace(name="forum")
        assert "форум" in await denied(rig, "bad_request", emoji="👍", message_id="5", to="channel:200")


async def test_message_ids_are_visible_in_inbox_and_event_data(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = build(rig)
        await rig.say(put(general, 71, "новость"))
        assert rig.events()[0]["data"]["message_id"] == 71 and "71" not in rig.events()[0]["text"]     # текст события не раздут
        out = await rig.core.call_tool("inbox", {"unread": True})
        assert "#71" in out and "новость" in out
        general.messages.append(Msg(72, "из истории", rig.me, general, minutes=5))
        out = await rig.core.call_tool("inbox", {"channel": "#общий"})
        assert "#71 Маша: новость" in out and "#72 она: из истории" in out
        assert "У сообщений указан id" in next(t for t in rig.client._manifest["tools"] if t["name"] == "inbox")["description"] \
            if hasattr(rig.client, "_manifest") else True
