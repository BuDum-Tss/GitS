"""События модуля Telegram: её аккаунт, вход getUpdates бота-витрины (ТЗ 10: раздел 6, TG-7, TG-9..TG-11)."""
import asyncio
import json
from types import SimpleNamespace

from tg_rig import Rig

OWNER = 777


def private_msg(uid=555, text="привет", chat=None, message_id=10, first="Маша", is_bot=False, **extra):
    return {"message_id": message_id, "chat": {"id": chat or uid, "type": "private"}, "text": text,
            "from": {"id": uid, "first_name": first, "is_bot": is_bot}, **extra}


async def test_inbox_loop_reads_updates_and_moves_offset(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.updates.append([
            {"update_id": 100, "message": private_msg(message_id=1, text="раз")},
            {"update_id": 101, "message": {"message_id": 2, "chat": {"id": -5, "type": "group"}, "text": "в группе"}},
            {"update_id": 102, "message": private_msg(message_id=3, text="три")},
        ])
        task = asyncio.create_task(rig.mod.channel.run_inbox())
        try:
            await rig.core.wait_for(lambda: len(rig.api_calls("sendMessage")) == 2)
            await rig.core.wait_for(lambda: (rig.mem / "telegram.json").exists())
        finally:
            task.cancel()
        assert rig.read_state()["update_offset"] == 103
        first = rig.api_calls("getUpdates")[0]
        assert first["allowed_updates"] == ["message"] and "offset" not in first
        assert rig.core.events == []                                           # бот-витрина событий в ядро не шлёт


async def test_inbox_does_not_poll_while_module_is_disabled(tmp_path, monkeypatch):
    """Выключенный модуль не отвечает: сообщения остаются в Telegram и обработаются после включения, не теряются."""
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.updates.append([{"update_id": 7, "message": private_msg(message_id=1)}])
        await rig.core.set_enabled(False)
        await rig.core.wait_for(lambda: not rig.client.enabled)
        task = asyncio.create_task(rig.mod.channel.run_inbox())
        await asyncio.sleep(0.3)
        assert rig.api_calls("getUpdates") == [] and rig.api_calls("sendMessage") == []
        await rig.core.set_enabled(True)
        await rig.core.wait_for(lambda: len(rig.api_calls("sendMessage")) == 1, timeout=8)
        task.cancel()


# ── её аккаунт (Telethon) ──

class FakeEvent:
    def __init__(self, *, text="привет", sender_id=555, chat_id=555, msg_id=1, private=True, group=False, title=None,
                 first="Маша", bot=False, reply_out=None, mentioned=False, media=None, out=False, username=None,
                 client=None):
        self.raw_text, self.sender_id, self.chat_id, self.id = text, sender_id, chat_id, msg_id
        self.out, self.client = out, client
        self.is_private, self.is_group, self.is_channel = private, group, not private and not group
        self.is_reply = reply_out is not None
        self.message = SimpleNamespace(mentioned=mentioned, **({media: True} if media else {}))      # media: voice, photo, sticker...
        self._sender = SimpleNamespace(first_name=first, last_name=None, username=username, bot=bot)
        self._chat = SimpleNamespace(title=title)
        self._reply = SimpleNamespace(out=reply_out) if reply_out is not None else None

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return self._chat

    async def get_reply_message(self):
        return self._reply


async def test_account_dm_event_and_inbox(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER}) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="ты скоро?", sender_id=OWNER, chat_id=OWNER, first="Аня"))
        ev = (await rig.core.wait_message("event")).body
        inbox = json.loads((rig.mem / "tg_inbox.json").read_text(encoding="utf-8"))
        rows = await rig.journal()
        unread = rig.mod.user.unread_count()
    assert ev["kind"] == "telegram.dm" and ev["text"] == "Сообщение от Аня в её личке: «ты скоро?»"
    assert ev["sender"] == {"id": f"tg:{OWNER}", "name": "Аня", "is_owner": True}
    assert ev["addressed_to_me"] is True and ev["memorable"] is True
    assert ev["reply_channel"] == {"module": "telegram", "target": f"user:{OWNER}", "tool": "telegram.text"}
    assert ev["dedupe_key"] == f"tg:dm:{OWNER}:1"
    assert inbox[-1]["from"] == "Аня" and inbox[-1]["text"] == "ты скоро?" and inbox[-1]["private"] is True    # TG-11
    assert any("написали в её аккаунт (личное): Аня: ты скоро?" in r["text"] for r in rows)
    assert unread == 1


async def test_account_group_event_addressed_rules(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._me = SimpleNamespace(first_name="Глэдис", username="glados_acc")
        base = dict(private=False, group=True, title="Соседи", chat_id=-100, sender_id=9, first="Петя")
        await user._on_incoming(FakeEvent(text="всем привет", msg_id=1, **base))
        await user._on_incoming(FakeEvent(text="ГЛаДОС, а ты что думаешь?", msg_id=2, **base))
        await user._on_incoming(FakeEvent(text="ответ", msg_id=3, reply_out=True, **base))
        await user._on_incoming(FakeEvent(text="ответ другому", msg_id=4, reply_out=False, **base))
        await user._on_incoming(FakeEvent(text="@glados_acc ау", msg_id=5, **base))
        await user._on_incoming(FakeEvent(text="тег", msg_id=6, mentioned=True, **base))
        await rig.core.wait_for(lambda: len(rig.core.events) == 6)
        evs = rig.core.events
    assert all(e["kind"] == "telegram.group" and e["memorable"] is False for e in evs)
    assert [e["addressed_to_me"] for e in evs] == [False, True, True, False, True, True]
    assert evs[0]["text"] == "Сообщение от Петя в группе «Соседи»: «всем привет»"
    assert evs[0]["reply_channel"] == {"module": "telegram", "target": "user:-100", "tool": "telegram.text"}


async def test_account_skips_service_bots_and_channels_but_keeps_inbox(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        await user._on_incoming(FakeEvent(text="Login code: 12345", sender_id=777000, chat_id=777000))
        await user._on_incoming(FakeEvent(text="реклама", bot=True))
        await user._on_incoming(FakeEvent(text="пост канала", private=False, group=False, title="Новости"))
        await asyncio.sleep(0.1)
        assert rig.core.events == []
        inbox = json.loads((rig.mem / "tg_inbox.json").read_text(encoding="utf-8"))
    assert len(inbox) == 3


async def test_inbox_unread_lists_inbox_and_resets_counter(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="первое"))
        await rig.mod.user._on_incoming(FakeEvent(text="второе", msg_id=2))
        assert rig.mod.user.unread_count() == 2
        out = await rig.core.call_tool("inbox", {"unread": True})
        assert "ДАННЫЕ ИЗ ЧАТОВ" in out and "первое" in out and "второе" in out
        assert rig.mod.user.unread_count() == 0
