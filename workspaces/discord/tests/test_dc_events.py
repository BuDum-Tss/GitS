"""События модуля Discord: discord.dm и discord.message, обращённость, вложения, лавина, продолжение диалога."""
import json
from types import SimpleNamespace

from dc_rig import OWNER_ID, Channel, Msg, Rig, User, reply_ref


def setup_guild(rig):
    g, (general,) = rig.server(1, "Клуб", (("общий", 100),))
    return g, general


async def test_channel_message_event_has_sender_reply_channel_and_data(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, ch = setup_guild(rig)
        await rig.say(Msg(1001, "всем привет", User(5, "masha", "Маша"), ch))
        [ev] = rig.events()
    assert ev["kind"] == "discord.message" and ev["text"] == "Сообщение от Маша в сервере «Клуб», канале «#общий»: «всем привет»"
    assert ev["addressed_to_me"] is False and ev["memorable"] is False
    assert ev["sender"] == {"id": "discord:5", "name": "Маша", "is_owner": False, "username": "@masha"}
    assert ev["reply_channel"] == {"module": "discord", "target": "channel:100"}
    assert ev["data"] == {"message_id": 1001, "guild_id": 1, "guild_name": "Клуб", "channel_id": 100, "channel_name": "общий"}
    assert ev["dedupe_key"] == "discord:message:100:1001"


async def test_addressed_by_mention_reply_and_name(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"names": "Глэдис, Стекло"}) as rig:
        g, ch = setup_guild(rig)
        masha = User(5, "masha", "Маша")
        await rig.say(Msg(1, "эй", masha, ch, mentions=[rig.me]))                                   # упоминание бота
        await rig.say(Msg(2, "да", masha, ch, reference=reply_ref(Msg(90, "ответ бота", rig.me, ch))))      # ответ на её сообщение
        await rig.say(Msg(3, "эй, стекло, ты тут?", masha, ch))                                      # имя из настройки names
        await rig.say(Msg(4, "ГЛаДОС, привет", masha, ch))                                          # имя персонажа от ядра
        await rig.say(Msg(5, "просто болтаем", masha, ch))                                           # мимо неё
        await rig.say(Msg(6, "ответ чужому", masha, ch, reference=reply_ref(Msg(91, "чужое", User(8, "kto"), ch))))
        await rig.say(Msg(7, "привет всем", masha, ch, mentions=[User(8, "kto")]))                   # упомянули другого
        got = [e["addressed_to_me"] for e in rig.events()]
    assert got == [True, True, True, True, False, False, False]


async def test_bots_own_webhook_and_system_messages_are_ignored(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, ch = setup_guild(rig)
        await rig.say(Msg(1, "от другого бота", User(9, "otherbot", bot=True), ch, mentions=[rig.me]))
        await rig.say(Msg(2, "моё же", rig.me, ch))
        await rig.say(Msg(3, "вебхук", User(10, "hook"), ch, webhook=77))
        await rig.say(Msg(4, "Маша зашла", User(5, "masha"), ch, mtype="new_member"))
        await rig.say(Msg(5, "ответ", User(5, "masha"), ch, mtype="reply"))                         # ответ это обычное сообщение
        assert [e["text"] for e in rig.events()] == ["Сообщение от masha в сервере «Клуб», канале «#общий»: «ответ»"]


async def test_thread_and_forum_post_have_own_target_and_parent_id(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, (general, forum) = rig.server(1, "Клуб", (("общий", 100), ("идеи", 200)))
        forum.type = SimpleNamespace(name="forum")
        thread = rig.dc.register(g.add(Channel(300, "обсуждение", "public_thread", parent=general)))
        post = rig.dc.register(g.add(Channel(400, "пост про игру", "public_thread", parent=forum)))
        await rig.say(Msg(1, "в ветке", User(5, "masha", "Маша"), thread))
        await rig.say(Msg(2, "в посте", User(5, "masha", "Маша"), post, mentions=[rig.me]))
        a, b = rig.events()
    assert a["reply_channel"]["target"] == "channel:300" and a["data"]["parent_id"] == 100 and a["data"]["channel_name"] == "обсуждение"
    assert "ветке «обсуждение» (канал «#общий»)" in a["text"]
    assert b["reply_channel"]["target"] == "channel:400" and b["data"]["parent_id"] == 200 and b["addressed_to_me"] is True


async def test_dm_event_is_always_addressed_and_memorable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        masha = User(5, "masha", "Маша")
        await rig.say(Msg(1, "привет, это личка", masha, Channel(9001, "dm", "dm", None, recipient=masha)))
        [ev] = rig.events()
    assert ev["kind"] == "discord.dm" and ev["addressed_to_me"] is True and ev["memorable"] is True
    assert ev["text"] == "Сообщение от Маша в её личке: «привет, это личка»"
    assert ev["reply_channel"] == {"module": "discord", "target": "dm:5"} and ev["sender"]["is_owner"] is False
    assert "guild_id" not in ev["data"]


async def test_owner_is_known_by_id_or_username_and_id_is_learned(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": "Аня_Вл"}) as rig:
        g, ch = setup_guild(rig)
        await rig.say(Msg(1, "я владелец", User(OWNER_ID, "аня_вл", "Аня"), ch))
        await rig.say(Msg(2, "не владелец", User(6, "petya", "Петя"), ch))
        a, b = rig.events()
        assert a["sender"]["is_owner"] is True and b["sender"]["is_owner"] is False
        assert rig.bot.owner.owner_id() == OWNER_ID                                  # id выучен по имени
    async with Rig(tmp_path / "second", monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        g, ch = setup_guild(rig)
        await rig.say(Msg(1, "по id", User(OWNER_ID, "кто-то", "Аня"), ch))
        assert rig.events()[0]["sender"]["is_owner"] is True


async def test_attachments_stickers_and_embeds_become_words_without_download(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, ch = setup_guild(rig)
        masha = User(5, "masha", "Маша")
        att = lambda ct, name="f": SimpleNamespace(content_type=ct, filename=name)          # noqa: E731
        await rig.say(Msg(1, "смотри", masha, ch, attachments=[att("image/png")]))
        await rig.say(Msg(2, "", masha, ch, attachments=[att("video/mp4")]))
        await rig.say(Msg(3, "", masha, ch, attachments=[att("audio/ogg")]))
        await rig.say(Msg(4, "", masha, ch, attachments=[att("application/zip", "a.zip")]))
        await rig.say(Msg(5, "", masha, ch, stickers=[SimpleNamespace(name="s")]))
        await rig.say(Msg(6, "", masha, ch, embeds=[SimpleNamespace(type="rich")]))
        await rig.say(Msg(7, "ссылка https://x", masha, ch, embeds=[SimpleNamespace(type="rich")]))        # предпросмотр без метки
        await rig.say(Msg(8, "", masha, ch, attachments=[att("image/gif", "a.gif")]))
        texts = [e["text"].split(": «", 1)[1].rstrip("»") for e in rig.events()]
    assert texts == ["[прислал картинку] смотри", "[прислал видео]", "[прислал аудио]", "[прислал файл]", "[прислал стикер]",
                     "[прислал эмбед]", "ссылка https://x", "[прислал гифку]"]


async def test_dm_flood_lowers_urgency_to_digest_but_never_owner_or_channel(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID), "dm_flood_per_10min": 3}) as rig:
        g, ch = setup_guild(rig)
        masha, owner = User(5, "masha", "Маша"), User(OWNER_ID, "anya", "Аня")
        dm = Channel(9001, "dm", "dm", None, recipient=masha)
        for i in range(5):
            await rig.say(Msg(i, f"спам {i}", masha, dm))
        for i in range(6):
            await rig.say(Msg(100 + i, f"владелец {i}", owner, Channel(9002, "dm", "dm", None, recipient=owner)))
        for i in range(6):
            await rig.say(Msg(200 + i, f"в канале {i}", masha, ch, mentions=[rig.me]))
        evs = rig.events()
    dms = evs[:5]
    assert [e["addressed_to_me"] for e in dms] == [True, True, True, False, False]
    assert [e.get("urgency_hint") for e in dms] == [None, None, None, 1, 1] and not any(e["memorable"] for e in dms[3:])
    assert all(e["addressed_to_me"] and e.get("urgency_hint") is None for e in evs[5:])      # лавина только для чужой лички


async def test_flood_off_when_zero(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"dm_flood_per_10min": 0}) as rig:
        masha = User(5, "masha", "Маша")
        for i in range(30):
            await rig.bot._on_incoming(Msg(i, "т", masha, Channel(9001, "dm", "dm", None, recipient=masha)))
        import asyncio
        await asyncio.sleep(0.2)
        assert all(e["addressed_to_me"] for e in rig.events())


async def test_dialog_continuation_in_channel_and_dm(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"dialog_window_mins": 15, "dm_flood_per_10min": 1}) as rig:
        g, ch = setup_guild(rig)
        masha, petya = User(5, "masha", "Маша"), User(6, "petya", "Петя")
        await rig.say(Msg(1, "ГЛаДОС, привет", masha, ch))                 # обратились: Маша её собеседник
        rig.bot.dialogs.note_out("channel:100")                              # она ответила в канале
        await rig.say(Msg(2, "а как дела?", masha, ch))                    # продолжение без упоминания
        await rig.say(Msg(3, "а у меня вот", petya, ch))                   # другой человек
        a, b, c = rig.events()
        assert b["addressed_to_me"] is True and b["data"]["continuation"] is True and c["addressed_to_me"] is False
        # личка: продолжение диалога лавиной не считается
        dm = Channel(9001, "dm", "dm", None, recipient=masha)
        await rig.say(Msg(10, "раз", masha, dm))
        rig.bot.dialogs.note_out("dm:5")
        for i in range(3):
            await rig.say(Msg(11 + i, "продолжаю", masha, dm))
        dm_events = rig.events("discord.dm")
    assert all(e["addressed_to_me"] for e in dm_events) and all(e.get("urgency_hint") is None for e in dm_events)


async def test_dialog_window_zero_turns_continuation_off(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"dialog_window_mins": 0}) as rig:
        g, ch = setup_guild(rig)
        masha = User(5, "masha", "Маша")
        await rig.say(Msg(1, "ГЛаДОС, привет", masha, ch))
        rig.bot.dialogs.note_out("channel:100")
        await rig.say(Msg(2, "а как дела?", masha, ch))
        assert [e["addressed_to_me"] for e in rig.events()] == [True, False]


async def test_activity_and_known_files_hold_ids_not_texts(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, ch = setup_guild(rig)
        await rig.say(Msg(1, "СЕКРЕТНЫЙ ТЕКСТ", User(5, "masha", "Маша"), ch, mentions=[rig.me]))
        rig.bot.dialogs.note_out("channel:100")
    for name in ("discord_dialogs.json", "discord_known.json"):
        raw = (rig.mem / name).read_text(encoding="utf-8")
        assert "СЕКРЕТНЫЙ" not in raw and json.loads(raw)
    assert rig.bot.dialogs.activity("channel:100")["hour"] == 1 and rig.bot.dialogs.activity("channel:100")["last_in"] > 0
