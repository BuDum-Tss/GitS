"""Её аккаунт как собеседник (Telethon): текст вместо вложений, обращённость, продолжение диалога, лавина в личке,
«печатает», прочитано, FloodWait и её собственная активность в inbox. Telethon подменён, сети нет."""
import time
from types import SimpleNamespace

import pytest
import telegram_user
from test_tg_events import FakeEvent
from tg_rig import Rig

from gits_sdk import ToolError

GROUP = dict(private=False, group=True, title="Соседи", chat_id=-100)


class FloodWaitError(Exception):
    """Как у Telethon: по имени класса и полю seconds."""

    def __init__(self, seconds):
        super().__init__(f"нужно подождать {seconds} с")
        self.seconds = seconds


async def events(rig, n):
    await rig.core.wait_for(lambda: len(rig.core.events) >= n)
    return rig.core.events


# ── вложения становятся текстом ──

@pytest.mark.parametrize("media,label", [("voice", "[прислал голосовое]"), ("photo", "[прислал картинку]"),
                                         ("sticker", "[прислал стикер]"), ("video_note", "[прислал видеокружок]")])
async def test_media_becomes_text_without_download(tmp_path, monkeypatch, media, label):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="", media=media))
        ev = (await events(rig, 1))[0]
    assert ev["text"] == f"Сообщение от Маша в её личке: «{label}»"


async def test_media_caption_is_kept(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="смотри", media="photo"))
        ev = (await events(rig, 1))[0]
    assert "[прислал картинку] смотри" in ev["text"]


# ── кого слушаем ──

async def test_own_outgoing_and_other_bots_are_ignored(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._me = SimpleNamespace(id=42, first_name="Глэдис", username="glados_acc")
        await user._on_incoming(FakeEvent(text="это я сама", out=True))
        await user._on_incoming(FakeEvent(text="и это я", sender_id=42))
        await user._on_incoming(FakeEvent(text="чужой бот", bot=True, **GROUP))
        await user._on_incoming(FakeEvent(text="живой человек", msg_id=5))
        evs = await events(rig, 1)
    assert [e["text"] for e in evs] == ["Сообщение от Маша в её личке: «живой человек»"]


async def test_sender_carries_username_and_owner_flag(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner": "@boss"}) as rig:
        await rig.mod.user._on_incoming(FakeEvent(sender_id=7, chat_id=7, first="Шеф", username="Boss"))
        await rig.mod.user._on_incoming(FakeEvent(sender_id=8, chat_id=8, first="Маша", username="masha", msg_id=2))
        a, b = await events(rig, 2)
    assert a["sender"] == {"id": "tg:7", "name": "Шеф", "is_owner": True, "username": "@boss"}
    assert b["sender"]["is_owner"] is False and b["sender"]["username"] == "@masha"
    assert a["reply_channel"] == {"module": "telegram", "target": "user:7", "tool": "telegram.text"} and a["addressed_to_me"] is True


# ── в группе обращаются по имени из настройки names ──

async def test_group_addressed_by_names_setting(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"names": "Глаша, Стекляшка"}) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="Глаша, привет", msg_id=1, sender_id=9, **GROUP))
        await user._on_incoming(FakeEvent(text="эй, стекляшка!", msg_id=2, sender_id=9, **GROUP))
        await user._on_incoming(FakeEvent(text="просто болтаем", msg_id=3, sender_id=9, **GROUP))
        evs = await events(rig, 3)
    assert [e["addressed_to_me"] for e in evs] == [True, True, False]


# ── помечает прочитанным ──

async def test_dm_and_addressed_group_marked_read_plain_group_is_not(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._client = rig.tl
        user._me = SimpleNamespace(id=1, first_name="Глэдис", username="glados_acc")
        await user._on_incoming(FakeEvent(text="личка", chat_id=5, sender_id=5, msg_id=11))
        await user._on_incoming(FakeEvent(text="всем привет", msg_id=12, sender_id=9, **GROUP))
        await user._on_incoming(FakeEvent(text="ГЛаДОС, ты тут?", msg_id=13, sender_id=9, **GROUP))
        await events(rig, 3)
    assert rig.tl.read_acks == [(5, 11), (-100, 13)]


# ── лавина в личке: единственный предохранитель ──

async def test_dm_flood_lowers_urgency_but_blocks_nothing(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"dm_flood_per_10min": 3}) as rig:
        for i in range(5):
            await rig.mod.user._on_incoming(FakeEvent(text=f"спам {i}", sender_id=66, chat_id=66, msg_id=i + 1))
        evs = await events(rig, 5)
    assert len(evs) == 5                                                       # ничего не заблокировано
    assert [e["addressed_to_me"] for e in evs] == [True, True, True, False, False]
    assert [e.get("urgency_hint") for e in evs] == [None, None, None, 1, 1]
    assert [e["memorable"] for e in evs] == [True, True, True, False, False]


async def test_dm_flood_does_not_touch_owner_or_other_senders_and_can_be_off(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True,
                   module_settings={"dm_flood_per_10min": 2, "owner_tg_id": 777}) as rig:
        user = rig.mod.user
        for i in range(4):
            await user._on_incoming(FakeEvent(text="я", sender_id=777, chat_id=777, msg_id=i + 1, first="Аня"))
        await user._on_incoming(FakeEvent(text="другой", sender_id=5, chat_id=5, msg_id=9))
        evs = await events(rig, 5)
    assert all(e["addressed_to_me"] is True and "urgency_hint" not in e for e in evs)
    async with Rig(tmp_path / "b", monkeypatch, telethon=True, module_settings={"dm_flood_per_10min": 0}) as rig:
        for i in range(30):
            await rig.mod.user._on_incoming(FakeEvent(text="ещё", sender_id=66, chat_id=66, msg_id=i + 1))
        evs = await events(rig, 30)
    assert all(e["addressed_to_me"] is True for e in evs)


async def test_group_messages_do_not_count_for_the_flood_limit(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"dm_flood_per_10min": 1}) as rig:
        for i in range(4):
            await rig.mod.user._on_incoming(FakeEvent(text="ГЛаДОС", msg_id=i + 1, sender_id=9, **GROUP))
        evs = await events(rig, 4)
    assert all(e["addressed_to_me"] is True and "urgency_hint" not in e for e in evs)


# ── продолжение диалога ──

async def test_group_continuation_after_her_reply(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="ГЛаДОС, привет", msg_id=1, sender_id=9, **GROUP))
        await user._on_incoming(FakeEvent(text="без тега до ответа", msg_id=2, sender_id=9, **GROUP))
        await user.reply(-100, "Привет, Петя.")                                 # она ответила в группе
        await user._on_incoming(FakeEvent(text="а ты умеешь шутить?", msg_id=3, sender_id=9, **GROUP))
        await user._on_incoming(FakeEvent(text="это другой человек", msg_id=4, sender_id=10, **GROUP))
        evs = await events(rig, 4)
    assert [e["addressed_to_me"] for e in evs] == [True, False, True, False]
    assert evs[2]["data"]["continuation"] is True and "continuation" not in evs[3]["data"]


async def test_continuation_expires_with_the_window(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="ГЛаДОС", msg_id=1, sender_id=9, **GROUP))
        await user.reply(-100, "Да?")
        user.dialogs._clock = lambda: time.time() + 16 * 60                     # прошло больше 15 минут
        await user._on_incoming(FakeEvent(text="поздно", msg_id=2, sender_id=9, **GROUP))
        evs = await events(rig, 2)
    assert evs[1]["addressed_to_me"] is False


async def test_continuation_window_is_a_setting(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"dialog_window_mins": 0}) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="ГЛаДОС", msg_id=1, sender_id=9, **GROUP))
        await user.reply(-100, "Да?")
        await user._on_incoming(FakeEvent(text="ещё", msg_id=2, sender_id=9, **GROUP))
        evs = await events(rig, 2)
    assert evs[1]["addressed_to_me"] is False


async def test_dm_in_dialog_is_not_counted_as_flood(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"dm_flood_per_10min": 1}) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="раз", sender_id=5, chat_id=5, msg_id=1))
        await user._on_incoming(FakeEvent(text="два", sender_id=5, chat_id=5, msg_id=2))      # лавина
        await user.reply(5, "Слушаю.")
        await user._on_incoming(FakeEvent(text="три", sender_id=5, chat_id=5, msg_id=3))      # она в диалоге: не лавина
        evs = await events(rig, 3)
    assert [e["addressed_to_me"] for e in evs] == [True, False, True]
    assert evs[2]["data"]["continuation"] is True


# ── естественность: «печатает» ──

def test_typing_time_grows_with_length_within_bounds():
    assert telegram_user.typing_seconds("") == 0.5
    assert 0.5 < telegram_user.typing_seconds("x" * 40) < 4.0
    assert telegram_user.typing_seconds("x" * 5000) == 4.0
    assert telegram_user.typing_seconds("x" * 5000, 0) == 0


async def test_typing_is_shown_before_every_message(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        rig.mod.user.typing_scale = 0.001
        await rig.mod.user.reply(5, "Привет!")
        await rig.mod.user.reply(5, "Как дела?")
        assert rig.tl.actions == [(("entity", 5), "typing")] * 2
        assert [t for _, t in rig.tl.sent] == ["Привет!", "Как дела?"]


# ── FloodWait ──

async def test_floodwait_waits_and_retries_once(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_user, "FLOOD_PAD_S", 0)
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        calls = []
        original = rig.tl.send_message

        async def flaky(ent, text, **kw):
            calls.append(text)
            if len(calls) == 1:
                raise FloodWaitError(0)
            return await original(ent, text, **kw)
        rig.tl.send_message = flaky
        await rig.mod.user.reply(5, "Привет")
        assert calls == ["Привет", "Привет"] and rig.tl.sent == [(("entity", 5), "Привет")]


async def test_floodwait_twice_becomes_tool_error(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_user, "FLOOD_PAD_S", 0)
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        async def entity(client, chat):
            return ("chat", -100)
        rig.mod.user._entity = entity
        rig.mod.user.known.add("соседи")
        calls = []

        async def always(ent, text, **kw):
            calls.append(text)
            raise FloodWaitError(0)
        rig.tl.send_message = always
        with pytest.raises(ToolError) as err:
            await rig.mod.message({"to": "Соседи", "text": "всем привет"})
        assert err.value.code == "unavailable" and "FloodWait" in str(err.value)
        assert len(calls) == 2                                                  # одна попытка и один повтор


async def test_floodwait_too_long_is_not_waited_for(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        calls = []

        async def long_wait(ent, text, **kw):
            calls.append(text)
            raise FloodWaitError(3600)
        rig.tl.send_message = long_wait
        with pytest.raises(RuntimeError, match="FloodWait"):
            await rig.mod.user.reply(5, "Привет")
        assert len(calls) == 1


# ── её активность вместо лимитов ──

async def test_inbox_chat_shows_her_own_activity_without_limits(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user

        async def entity(client, chat):
            return ("chat", -100)

        async def get_messages(ent, limit=15):
            return []
        rig.mod.user._entity = entity
        rig.tl.get_messages = get_messages
        await user._on_incoming(FakeEvent(text="ГЛаДОС", msg_id=1, sender_id=9, **GROUP))
        for _ in range(3):
            await user.reply(-100, "ответ")
        out = await rig.core.call_tool("inbox", {"chat": "Соседи"})
    assert "ТВОЯ АКТИВНОСТЬ" in out and "за последний час ты написала 3, за сутки 3" in out and "Лимитов нет" in out
    assert "последнее сообщение тебе: " in out and "ещё не было" not in out


def test_dialogs_count_hour_and_day_separately(tmp_path):
    from common import Dialogs

    now = [1_000_000.0]
    d = Dialogs(str(tmp_path), clock=lambda: now[0])
    d.note_out(5)
    now[0] += 7200
    d.note_out(5)
    d.note_out(5)
    assert d.activity(5)["hour"] == 2 and d.activity(5)["day"] == 3
    now[0] += 86400
    assert d.activity(5)["day"] == 0 and d.activity(6) == {"hour": 0, "day": 0, "last_out": 0.0, "last_in": 0.0}
