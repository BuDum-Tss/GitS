"""Бот-витрина: /start и /help с кнопками, подписка и отписка, рассылка объявлений, ответ-заглушка без событий."""
import json

import pytest
from tg_rig import Rig

from gits_sdk import ToolError

LINKS = {"channel_url": "https://t.me/glados_channel", "account_url": "https://t.me/glados_acc",
         "twitch_url": "https://twitch.tv/glados", "youtube_url": "https://youtube.com/@glados"}


def private(text, uid=555, first="Маша", message_id=1, **extra):
    return {"message_id": message_id, "chat": {"id": uid, "type": "private"}, "text": text,
            "from": {"id": uid, "first_name": first, "is_bot": False}, **extra}


def last_send(rig):
    return rig.api_calls("sendMessage")[-1]


def buttons(markup):
    return [(b["text"], b["url"]) for row in markup["inline_keyboard"] for b in row]


@pytest.mark.parametrize("command", ["/start", "/help", "/start@glados_bot", "/START"])
async def test_start_and_help_show_about_and_all_link_buttons(tmp_path, monkeypatch, command):
    async with Rig(tmp_path, monkeypatch, module_settings={**LINKS, "about": "Я ГЛаДОС. Витрина."}) as rig:
        assert await rig.mod.channel._on_private(private(command))
        sent = last_send(rig)
    assert sent["chat_id"] == 555 and sent["text"] == "Я ГЛаДОС. Витрина."
    assert buttons(sent["reply_markup"]) == [("Telegram-канал", LINKS["channel_url"]), ("Telegram-аккаунт", LINKS["account_url"]),
                                             ("Twitch", LINKS["twitch_url"]), ("YouTube", LINKS["youtube_url"])]
    assert rig.core.events == []


async def test_empty_or_bad_link_means_no_button_and_default_about_is_used(tmp_path, monkeypatch):
    settings = {"channel_url": "", "account_url": "https://t.me/glados_acc", "twitch_url": "не ссылка", "about": "  "}
    async with Rig(tmp_path, monkeypatch, module_settings=settings) as rig:
        await rig.mod.channel._on_private(private("/start"))
        sent = last_send(rig)
        assert buttons(sent["reply_markup"]) == [("Telegram-аккаунт", "https://t.me/glados_acc")]
        assert "ГЛаДОС" in sent["text"]                                       # дефолтное описание
    async with Rig(tmp_path / "b", monkeypatch) as rig:                       # ссылок нет совсем: сообщение без кнопок
        await rig.mod.channel._on_private(private("/help"))
        assert "reply_markup" not in last_send(rig)


async def test_subscribe_and_unsubscribe_are_stored_in_one_file(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        subs = rig.mod.channel.subscribers
        await rig.mod.channel._on_private(private("/subscribe"))
        assert subs.ids() == [555] and "подписаны" in last_send(rig)["text"]
        await rig.mod.channel._on_private(private("/subscribe"))              # повторно: ничего не меняется
        assert subs.count() == 1 and "уже подписаны" in last_send(rig)["text"]
        data = json.loads((rig.mem / "tg_subscribers.json").read_text(encoding="utf-8"))
        assert data["chats"]["555"]["name"] == "Маша" and not list(rig.mem.glob("*.tmp"))
        await rig.mod.channel._on_private(private("/unsubscribe"))
        assert subs.ids() == [] and "снята" in last_send(rig)["text"]
        await rig.mod.channel._on_private(private("/unsubscribe"))
        assert "не были подписаны" in last_send(rig)["text"]
        assert rig.core.events == []


async def test_subscribers_survive_restart(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await rig.mod.channel._on_private(private("/subscribe", uid=7))
    async with Rig(tmp_path, monkeypatch) as rig2:
        assert rig2.mod.channel.subscribers.ids() == [7]


async def test_any_other_message_gets_one_stub_with_account_button_and_no_events(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={**LINKS, "owner_tg_id": 777}) as rig:
        for uid, text in ((555, "привет, расскажи шутку"), (777, "как дела?"), (555, "/unknown"), (555, "")):
            before = len(rig.api_calls("sendMessage"))
            assert await rig.mod.channel._on_private(private(text, uid=uid))
            assert len(rig.api_calls("sendMessage")) == before + 1            # ровно один ответ
            sent = last_send(rig)
            assert sent["chat_id"] == uid and "только объявляю" in sent["text"] and len(sent["text"]) < 100
            assert buttons(sent["reply_markup"]) == [("Telegram-аккаунт", LINKS["account_url"])]
        await rig.mod.channel._on_private({"message_id": 9, "chat": {"id": 5, "type": "private"},
                                           "from": {"id": 5, "first_name": "Паша"}, "photo": []})      # не текст
        assert "только объявляю" in last_send(rig)["text"]
        assert rig.core.events == [] and not rig.mod.channel.subscribers.ids()
        assert not any(m == "event" for m in [x.type for x in rig.core.received if hasattr(x, "type")]) \
            if hasattr(rig.core, "received") else True


async def test_bot_messages_are_ignored_by_the_showcase(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert await rig.mod.channel._on_private({**private("/start"), "from": {"id": 5, "is_bot": True}})
        assert rig.api_calls("sendMessage") == []


async def test_failed_stub_reply_does_not_break_the_inbox(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        async def broken(method, _timeout=15, **params):
            raise RuntimeError("Forbidden: bot was blocked by the user")
        rig.mod.channel._call = broken
        assert await rig.mod.channel._on_private(private("привет"))


# ── рассылка ──

async def subscribe(rig, *ids):
    for uid in ids:
        await rig.mod.channel._on_private(private("/subscribe", uid=uid))
    rig.api.clear()


async def test_announce_sends_text_and_link_to_every_subscriber_with_pause(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await subscribe(rig, 1, 2, 3)
        pauses = []

        async def fake_sleep(sec):
            pauses.append(sec)
        monkeypatch.setattr("telegram_channel.asyncio.sleep", fake_sleep)
        out = await rig.core.call_tool("announce", {"text": "Начался стрим!", "url": "https://twitch.tv/glados"})
    assert out == "объявление отправлено: 3 из 3"
    sends = [(p["chat_id"], p["text"]) for p in rig.api_calls("sendMessage")]
    assert sends == [(i, "Начался стрим!\nhttps://twitch.tv/glados") for i in (1, 2, 3)]
    assert [p for p in pauses if p] == [pytest.approx(0.05)] * 2                                  # 20 сообщений в секунду, между сообщениями


async def test_announce_removes_those_who_blocked_the_bot(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await subscribe(rig, 1, 2, 3, 4)
        rig.mod.channel.announce_gap = 0
        orig = rig.mod.channel._call

        async def flaky(method, _timeout=15, **params):
            if method == "sendMessage" and params["chat_id"] == 2:
                raise RuntimeError("Forbidden: bot was blocked by the user")
            if method == "sendMessage" and params["chat_id"] == 3:
                raise RuntimeError("Too Many Requests: retry after 5")         # временный сбой: подписка остаётся
            return await orig(method, _timeout, **params)
        rig.mod.channel._call = flaky
        out = await rig.core.call_tool("announce", {"text": "Вышло видео"})
        assert rig.mod.channel.subscribers.ids() == [1, 3, 4]
    assert out.startswith("объявление отправлено: 2 из 4")
    assert "заблокировали бота): 1" in out and "сбоев: 1" in out


async def test_announce_without_subscribers_or_token_and_with_bad_arguments(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert "подписчиков пока нет" in await rig.core.call_tool("announce", {"text": "Стрим"})
        await subscribe(rig, 1)
        for args in ({}, {"text": "  "}, {"text": "x" * 1001}, {"text": "Стрим", "url": "javascript:alert(1)"}):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool("announce", args)
            assert e.value.code == "bad_request"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    async with Rig(tmp_path / "t", monkeypatch, token=None) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("announce", {"text": "Стрим"})
        assert e.value.code == "unavailable"
