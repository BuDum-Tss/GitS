"""Режим «разговоры ведёт бот» (account_mode: bot): события из update Bot API, отправка через бота, единый поллер, буфер.

Сети нет: `_call` и `_upload` канала подменены стендом (tg_rig), токен подставной, Telethon не нужен."""
import asyncio
import json
import sys
import time

import pytest
from common import ACCOUNT_MODES, account_mode
from manifest import MANIFEST
from telegram_bot_user import SHOWCASE_COMMANDS, BotBuffer, TelegramBotUser
from telegram_user import ChatModeError, TelegramUser
from tg_rig import Rig

from gits_sdk import ToolError
from gits_sdk import protocol as P

OWNER = 777
TOKEN = "123:SECRET-TOKEN"
SUPER = -100500


def bot_rig(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("GITS_TG_ACCOUNT_MODE", "bot")
    kw.setdefault("module_settings", {"owner_tg_id": OWNER})
    return Rig(tmp_path, monkeypatch, **kw)


async def ready(rig):
    await rig.mod.user._identify()                                  # getMe: имя бота нужно для обращённости
    return rig.mod.user


def dm(text="привет", uid=555, mid=10, first="Маша", **extra):
    return {"update_id": mid, "message": {"message_id": mid, "chat": {"id": uid, "type": "private"}, "text": text, "date": int(time.time()),
                                           "from": {"id": uid, "first_name": first, "is_bot": False}, **extra}}


def grp(text="всем привет", uid=9, chat=-100, mid=1, title="Соседи", **extra):
    return {"update_id": mid, "message": {"message_id": mid, "chat": {"id": chat, "type": "supergroup", "title": title},
                                           "text": text, "date": int(time.time()), "from": {"id": uid, "first_name": "Петя", "is_bot": False},
                                           **extra}}


async def events(rig, n):
    await rig.core.wait_for(lambda: len(rig.core.events) >= n)
    return rig.core.events


# ── выбор режима ──

def test_manifest_has_account_mode_setting_default_user():
    spec = MANIFEST["settings_schema"]["account_mode"]
    assert spec["default"] == "user" and spec["enum"] == list(ACCOUNT_MODES) == ["user", "bot"]
    assert "тоглом" in spec["title"] and "GITS_TG_ACCOUNT_MODE" in spec["title"]
    from manifest import build_manifest
    assert P.validate_manifest(build_manifest()) == []


def test_account_mode_setting_wins_env_is_fallback(monkeypatch):
    monkeypatch.delenv("GITS_TG_ACCOUNT_MODE", raising=False)
    assert account_mode(None) == "user" and account_mode({}) == "user" and account_mode({"account_mode": "ерунда"}) == "user"
    assert account_mode({"account_mode": "bot"}) == "bot"
    monkeypatch.setenv("GITS_TG_ACCOUNT_MODE", "bot")
    assert account_mode({"account_mode": "user"}) == "user" and account_mode(None) == "bot" and account_mode({}) == "bot"
    monkeypatch.setenv("GITS_TG_ACCOUNT_MODE", "user")                      # «user» из окружения настройку не перекрывает
    assert account_mode({"account_mode": "bot"}) == "bot"


async def test_default_mode_is_the_account(tmp_path, monkeypatch):
    monkeypatch.delenv("GITS_TG_ACCOUNT_MODE", raising=False)
    async with Rig(tmp_path, monkeypatch) as rig:
        await rig.mod._ensure_mode()
        assert type(rig.mod.user) is TelegramUser and not rig.mod.user.is_bot_mode and rig.mod.channel.bot_polls is False
        assert rig.mod.panel.user is rig.mod.user


async def test_env_selects_the_bot_before_the_core_connects(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        assert isinstance(rig.mod.user, TelegramBotUser) and rig.mod.channel.bot_polls is True
        assert rig.mod.panel.user is rig.mod.user and rig.mod.user.on_change is not None


async def test_setting_selects_the_bot_when_the_core_sends_it(tmp_path, monkeypatch):
    monkeypatch.delenv("GITS_TG_ACCOUNT_MODE", raising=False)
    async with Rig(tmp_path, monkeypatch, module_settings={"account_mode": "bot"}) as rig:
        old = rig.mod.user
        assert type(old) is TelegramUser
        await rig.mod._ensure_mode()
        user = rig.mod.user
        assert isinstance(user, TelegramBotUser) and user.typing_scale == 0 and rig.mod.panel.user is user
        assert "glados" in user._own_names()                                 # алиасы персонажа перенесены
        assert rig.mod.channel.bot_polls is True


async def test_run_inbox_does_not_poll_in_bot_mode(tmp_path, monkeypatch):
    """Единый поллер: канал-витрина в режиме бота getUpdates не опрашивает (иначе Telegram ответит 409)."""
    async with bot_rig(tmp_path, monkeypatch) as rig:
        task = asyncio.create_task(rig.mod.channel.run_inbox())
        await asyncio.wait_for(task, 2)                                      # вернулся сразу, а не завис в цикле
        assert rig.api_calls("getUpdates") == []


# ── события ──

async def test_owner_dm_becomes_a_telegram_dm_event(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        await user.handle_update(dm("ты скоро?", uid=OWNER, first="Аня"))
        ev = (await events(rig, 1))[0]
        assert rig.api_calls("sendMessage") == []                            # заглушки нет: это разговор
    assert ev["kind"] == "telegram.dm" and ev["text"] == "Сообщение от Аня в её личке: «ты скоро?»"
    assert ev["sender"]["is_owner"] is True and ev["addressed_to_me"] is True and ev["memorable"] is True
    assert ev["reply_channel"] == {"module": "telegram", "target": f"user:{OWNER}", "tool": "telegram.text"}
    assert ev["data"]["group_key"] == f"tg:{OWNER}:{OWNER}" and ev["data"]["chat_id"] == OWNER
    assert ev["dedupe_key"] == f"tg:dm:{OWNER}:10"


async def test_group_message_needs_an_address_mention_or_reply_to_the_bot(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        await user.handle_update(grp("всем привет", mid=1))
        await user.handle_update(grp("@glados_bot ты тут?", mid=2, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        await user.handle_update(grp("ответ", mid=3, reply_to_message={"message_id": 70, "from": {"id": 42}}))
        await user.handle_update(grp("ответ другому", mid=4, reply_to_message={"message_id": 71, "from": {"id": 5}}))
        await user.handle_update(grp("ГЛаДОС, а ты что думаешь?", mid=5))
        evs = await events(rig, 5)
    assert all(e["kind"] == "telegram.group" and e["memorable"] is False for e in evs)
    assert [e["addressed_to_me"] for e in evs] == [False, True, True, False, True]
    assert evs[0]["urgency_hint"] == 1 and "urgency_hint" not in evs[1]       # болтовня: фон; обращение: без подсказки
    assert evs[1]["data"]["group_key"] == "tg:-100:9"
    assert evs[1]["reply_channel"] == {"module": "telegram", "target": "user:-100", "tool": "telegram.text"}


async def test_forum_topic_event_and_reply_goes_to_the_same_topic(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        topic = dict(message_thread_id=5, is_topic_message=True, reply_to_message={
            "message_id": 5, "forum_topic_created": {"name": "ПРОГРЕСС"}, "from": {"id": 9}})
        upd = grp("@glados_bot как дела", chat=SUPER, title="Клуб", mid=20, entities=[{"type": "mention", "offset": 0, "length": 11}],
                  **topic)
        upd["message"]["chat"]["is_forum"] = True
        await user.handle_update(upd)
        ev = (await events(rig, 1))[0]
        assert ev["addressed_to_me"] is True                                  # служебный reply_to_message не считается ответом боту
        assert ev["data"]["topic_id"] == 5 and ev["data"]["topic_title"] == "ПРОГРЕСС"
        assert ev["reply_channel"]["target"] == f"user:{SUPER}#5"
        assert (await rig.core.request("deliver", {"target": f"user:{SUPER}#5", "text": "ответ"})) == {"delivered": True}
        assert rig.api_calls("sendMessage")[-1] == {"chat_id": SUPER, "text": "ответ", "message_thread_id": 5}
        await rig.core.call_tool("text", {"to": "Клуб / ПРОГРЕСС", "text": "ещё раз", "reply_to": 20})    # по названию темы
        last = rig.api_calls("sendMessage")[-1]
        assert last["message_thread_id"] == 5 and last["reply_parameters"] == {"message_id": 20, "allow_sending_without_reply": True}
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "Клуб / Нет такой", "text": "?"})
        assert e.value.code == "bad_request"


async def test_read_mode_does_not_reply_and_ignore_is_silent(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        user.modes.set(-100, "read")
        user.modes.set(-101, "ignore")
        await user.handle_update(grp("@glados_bot ау", mid=1, entities=[{"type": "mention", "offset": 0, "length": 11}]))
        await user.handle_update(grp("@glados_bot ау", chat=-101, title="Тихая", mid=2,
                                     entities=[{"type": "mention", "offset": 0, "length": 11}]))
        evs = await events(rig, 1)
        await asyncio.sleep(0.1)
        assert len(rig.core.events) == 1                                      # из игнорируемого чата ни одного события
        assert evs[0]["addressed_to_me"] is False and "reply_channel" not in evs[0] and evs[0]["data"]["chat_mode"] == "read"
        with pytest.raises(ChatModeError):
            await user.send("-100", "нельзя")
        with pytest.raises(ChatModeError):
            await user.reply(-101, "нельзя")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "-100", "text": "нельзя"})
        assert e.value.code == "denied" and rig.api_calls("sendMessage") == []
        rows = {r["id"]: r for r in await user.dialogs_view()}
        assert rows[-101]["mode"] == "ignore" and "text" not in json.dumps(user.buffer.messages(-101))   # режим вернуть можно, текста нет
        assert user.buffer.messages(-101) == []


async def test_default_chat_mode_applies_to_new_chats_and_strangers_in_private(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER, "default_chat_mode": "read"}) as rig:
        user = await ready(rig)
        await user.handle_update(dm("привет", uid=555, mid=1))
        await user.handle_update(dm("привет", uid=OWNER, mid=2, first="Аня"))
        evs = await events(rig, 2)
    assert evs[0]["addressed_to_me"] is False and "reply_channel" not in evs[0]      # чужой: режим по умолчанию
    assert evs[1]["addressed_to_me"] is True and "reply_channel" in evs[1]           # владелец: всегда reply


# ── витрина ──

async def test_showcase_commands_are_handled_by_the_showcase_not_as_a_conversation(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER, "about": "Я витрина"}) as rig:
        user = await ready(rig)
        assert set(SHOWCASE_COMMANDS) == {"/start", "/help", "/subscribe", "/unsubscribe"}
        await user.handle_update(dm("/start", uid=555, mid=1))
        await user.handle_update(dm("/subscribe@glados_bot", uid=555, mid=2))
        await user.handle_update(dm("/help", uid=OWNER, mid=3, first="Аня"))      # у владельца команды витрины тоже витрина
        await asyncio.sleep(0.1)
        sent = rig.api_calls("sendMessage")
        assert sent[0]["text"] == "Я витрина" and sent[0]["chat_id"] == 555
        assert "подписаны" in sent[1]["text"] and rig.mod.channel.subscribers.has(555)
        assert sent[2]["chat_id"] == OWNER
        assert rig.core.events == []                                           # ни одно не стало разговором
        await user.handle_update(dm("обычное сообщение", uid=555, mid=4))
        ev = (await events(rig, 1))[0]
        assert ev["kind"] == "telegram.dm" and len(rig.api_calls("sendMessage")) == 3    # и заглушки в ответ нет


# ── единый поллер ──

async def test_single_poller_handles_updates_and_moves_the_offset(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        rig.updates.append([dm("раз", uid=OWNER, mid=1, first="Аня") | {"update_id": 100}, {"update_id": 101, "poll": {}},
                            grp("два", mid=2) | {"update_id": 102}])
        task = asyncio.create_task(rig.mod.user.run())
        try:
            await events(rig, 2)
            await rig.core.wait_for(lambda: (rig.mem / "telegram.json").exists())
        finally:
            task.cancel()
        assert rig.read_state()["update_offset"] == 103
        first = rig.api_calls("getUpdates")[0]
        assert first["allowed_updates"] == ["message", "edited_message", "my_chat_member"] and "offset" not in first
        assert rig.api_calls("getMe") and rig.mod.user.state_word() == "бот @glados_bot: работает"


async def test_poller_does_not_poll_while_the_module_is_disabled(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        rig.updates.append([dm(mid=7)])
        await rig.core.set_enabled(False)
        await rig.core.wait_for(lambda: not rig.client.enabled)
        task = asyncio.create_task(rig.mod.user.run())
        await asyncio.sleep(0.3)
        assert rig.api_calls("getUpdates") == []
        task.cancel()


# ── чаты, добавление бота ──

async def test_my_chat_member_remembers_the_chat_and_logs_it(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)

        def member(status, who, chat=-200, title="Новая"):
            return {"update_id": 1, "my_chat_member": {"chat": {"id": chat, "type": "supergroup", "title": title},
                                                       "from": who, "new_chat_member": {"status": status, "user": {"id": 42}}}}
        await user.handle_update(member("member", {"id": OWNER, "first_name": "Аня"}))
        await user.handle_update(member("administrator", {"id": 9, "first_name": "Чужой"}, chat=-201, title="Чужая"))
        rows = {r["id"]: r for r in await user.dialogs_view()}
        assert rows[-200]["name"] == "Новая" and rows[-200]["mode"] == "reply" and rows[-201]["name"] == "Чужая"
        assert user.known.has("Новая") and not user.known.has("Чужая")           # «известной» делает только владелец
        texts = [e["text"] for e in await rig.journal()]
        assert any("бота добавили в супергруппа «Новая»" in t and "владелец" in t for t in texts)
        assert any("«Чужая»" in t and "режим нового чата" in t for t in texts)
        await user.handle_update(member("kicked", {"id": OWNER, "first_name": "Аня"}))
        assert -200 not in {r["id"]: r for r in await user.dialogs_view()}         # убрали: из списка уходит
        assert any("убрали" in e["text"] for e in await rig.journal())


async def test_default_chat_mode_of_a_new_chat_follows_the_setting(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER, "default_chat_mode": "ignore"}) as rig:
        user = await ready(rig)
        await user.handle_update({"update_id": 1, "my_chat_member": {
            "chat": {"id": -300, "type": "group", "title": "Г"}, "from": {"id": 9}, "new_chat_member": {"status": "member"}}})
        assert {r["id"]: r for r in await user.dialogs_view()}[-300]["mode"] == "ignore"


# ── отправка и ошибки ──

async def test_bot_cannot_write_first_gives_a_clear_tool_error(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        await ready(rig)
        orig = rig.mod.channel._call

        async def call(method, _timeout=15, **params):
            if method == "sendMessage":
                raise RuntimeError("Forbidden: bot can't initiate conversation with a user")
            return await orig(method, _timeout, **params)
        rig.mod.channel._call = call
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "5551234", "text": "привет"})
        assert e.value.code == "unavailable" and "первым" in e.value.message and "/start" in e.value.message

        async def missing(method, _timeout=15, **params):
            raise RuntimeError("Bad Request: chat not found")
        rig.mod.channel._call = missing
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "5551234", "text": "привет"})
        assert e.value.code == "bad_request" and "не знает" in e.value.message


async def test_long_text_is_split_into_telegram_sized_messages(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        text = ("слово " * 1500).strip()                                       # 8999 символов
        await user.send("555", text)
        parts = [c["text"] for c in rig.api_calls("sendMessage")]
        assert len(parts) == 3 and all(len(p) <= 4096 for p in parts) and " ".join(parts).split() == text.split()
        assert [m["text"] for m in user.buffer.messages(555)][0] == parts[0][:600]


async def test_typing_and_voice_and_files_go_through_the_bot(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        user.typing_scale = 0.01
        await user.send("555", "привет", ogg=b"OggS", reply_to=3)
        assert rig.api_calls("sendChatAction") == []                           # голосовое: текст ушёл подписью
        method, data, files = rig.uploads[-1]
        assert method == "sendVoice" and data["caption"] == "привет" and files["voice"][1] == b"OggS"
        assert json.loads(data["reply_parameters"])["message_id"] == 3
        await user.send("555", "текстом")
        assert rig.api_calls("sendChatAction")[-1]["action"] == "typing"
        pic = tmp_path / "a.png"
        pic.write_bytes(b"png")
        await user.send_files("555", [(pic, "photo")], "подпись")
        method, data, files = rig.uploads[-1]
        assert method == "sendPhoto" and data["caption"] == "подпись" and files["photo"] == ("a.png", b"png")


@pytest.mark.skip(reason="проверка приватности отключена владельцем")
async def test_leaks_are_refused_but_the_owner_panel_may_send(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "555", "text": "Привет, Анна"})
        assert e.value.code == "denied" and rig.api_calls("sendMessage") == []
        await user.send("555", "Привет, Анна", by_owner=True)                  # ручная отправка владельцем из панели
        assert len(rig.api_calls("sendMessage")) == 1


async def test_token_never_reaches_errors_or_the_journal(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        await ready(rig)

        async def call(method, _timeout=15, **params):
            raise RuntimeError(f"Client error for https://api.telegram.org/bot{TOKEN}/{method}: {TOKEN}")
        rig.mod.channel._call = call
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "555", "text": "привет"})
        assert "SECRET-TOKEN" not in e.value.message and "123:" not in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": "user:555", "text": "привет"})
        assert "SECRET-TOKEN" not in str(e.value)
        rig.mod.user._fail(RuntimeError(f"{TOKEN} unauthorized"))
        assert "SECRET-TOKEN" not in rig.mod.user.state()[1] and "недействителен" in rig.mod.user.state()[1]
        assert "SECRET-TOKEN" not in json.dumps(await rig.journal()) + json.dumps(await rig.journal("error"))


# ── чтение по буферу ──

async def test_reading_works_from_the_bots_own_buffer_and_says_so(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        await user.handle_update(grp("первое сообщение", mid=1))
        await user.handle_update(grp("второе", mid=2))
        await user.send("-100", "мой ответ")
        out = await rig.core.call_tool("inbox", {"chat": "Соседи"})
        assert "ДАННЫЕ ИЗ ЧАТА" in out and "первое сообщение" in out and "она: мой ответ" in out and "с момента" in out
        assert "Соседи" in await rig.core.call_tool("inbox", {})
        unread = await rig.core.call_tool("inbox", {"unread": True})
        assert "первое сообщение" in unread and "с момента" in unread
        await user.handle_update({"update_id": 9, "edited_message": grp("исправлено", mid=2)["message"]})
        assert "исправлено" in await rig.core.call_tool("inbox", {"chat": "-100"})
        history = await user.history(-100, 10)
        assert [m["text"] for m in history][:2] == ["первое сообщение", "исправлено"] and history[-1]["out"] is True


def test_buffer_is_bounded_and_atomic(tmp_path):
    clock = [1000.0]
    buf = BotBuffer(str(tmp_path), clock=lambda: clock[0])
    buf.note_chat(1, "Чат", "group")
    for i in range(BotBuffer.PER_CHAT + 30):
        buf.add_message(1, i, "x" * 2000, "кто", False)
    rows = buf.messages(1, limit=1000)
    assert len(rows) == BotBuffer.PER_CHAT and rows[-1]["id"] == BotBuffer.PER_CHAT + 29 and len(rows[0]["text"]) == 600
    clock[0] += BotBuffer.KEEP_S + 10                                          # всё устарело
    buf.add_message(1, 999, "новое", "кто", False)
    assert [m["id"] for m in buf.messages(1)] == [999]
    assert not list(tmp_path.glob("*.tmp"))


# ── состояние и недоступное ──

async def test_state_words_and_snapshot(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch, token=None) as rig:
        user = rig.mod.user
        assert user.state_word() == "бот: нет токена" and user.state()[0] is False
        summary, data = await asyncio.to_thread(rig.mod.snapshot)
        assert "разговоры: бот: нет токена" in summary and data["account_ok"] is False and data["account_mode"] == "bot"
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("text", {"to": "555", "text": "привет"})
        assert e.value.code == "unavailable" and "TELEGRAM_BOT_TOKEN" in e.value.message
    async with bot_rig(tmp_path / "b", monkeypatch) as rig:
        user = await ready(rig)
        ok, why = user.state()
        assert ok and "@glados_bot" in why and "privacy mode" in why                 # в getMe нет can_read_all_group_messages
        user._offline = True
        assert user.state_word() == "бот: нет связи"
        user._offline = False
        summary, data = await asyncio.to_thread(rig.mod.snapshot)
        assert summary.startswith("канал: не проверен, разговоры: бот @glados_bot: работает") and data["account_ok"] is True
        assert data["bot_username"] == "glados_bot" and data["bot_reads_groups"] is False


async def test_telethon_only_features_answer_politely_and_the_panel_lives(tmp_path, monkeypatch):
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        await user.handle_update(grp("привет", mid=1))
        for fn in (user.start_login, user.logout):
            with pytest.raises(RuntimeError) as e:
                await fn()
            assert "режиме бота" in str(e.value)
        assert user.submit_login_password("x") is False and await user.avatar_file(1, None, [5]) is None
        res = await rig.core.panel_action("login_start", {})
        assert res["ok"] is False and "режиме бота" in res["message"]
        blocks = await asyncio.wait_for(rig.mod.panel_blocks(), 5)
        assert blocks[0]["title"].startswith("Бот") and "бот @glados_bot: работает" in blocks[0]["text"]
        assert any(b.get("type") == "split" for b in blocks)                         # список чатов из буфера
        res = await rig.core.panel_action("send_message", {"chat": -100, "value": "из панели"})
        assert res["ok"] is True and rig.api_calls("sendMessage")[-1]["text"] == "из панели"


async def test_bot_mode_does_not_import_or_need_telethon(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "telethon", None)                              # import telethon -> ImportError
    async with bot_rig(tmp_path, monkeypatch) as rig:
        user = await ready(rig)
        await user.handle_update(dm("привет", uid=OWNER, first="Аня"))
        await events(rig, 1)
        await user.send(str(OWNER), "ответ")
        assert user.state()[0] is True and user.state_word().endswith("работает")
        assert (await rig.core.call_tool("inbox", {})) and rig.api_calls("sendMessage")


async def test_switch_mode_hot_swaps_user_and_back(tmp_path, monkeypatch):
    monkeypatch.delenv("GITS_TG_ACCOUNT_MODE", raising=False)
    async with Rig(tmp_path, monkeypatch, module_settings={}) as rig:
        await rig.mod._ensure_mode()
        assert not rig.mod.user.is_bot_mode
        assert await rig.mod.switch_mode("bot") and rig.mod.user.is_bot_mode and rig.mod.channel.bot_polls
        assert rig.mod.panel.user is rig.mod.user
        assert not await rig.mod.switch_mode("bot")
        assert await rig.mod.switch_mode("user") and not rig.mod.user.is_bot_mode and not rig.mod.channel.bot_polls
