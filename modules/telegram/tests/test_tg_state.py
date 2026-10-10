"""Состояние модуля для дашборда (ТЗ 10: TG-21) и поведение при выключении."""
import asyncio
import time
from types import SimpleNamespace

import app as app_mod
from tg_rig import Rig

POST = "Объявление для сотрудников: кофе закончился, эксперименты отменяются."


async def test_state_when_nothing_is_configured(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None) as rig:
        summary, data = await asyncio.to_thread(rig.mod.snapshot)
    assert summary == "канал: не настроен, аккаунт: нет ключей, последний пост: ещё не было"
    assert data == {"channel_ok": False, "account_ok": False, "last_post_ts": 0.0, "unread": 0}


async def test_state_ready_channel_and_connected_account(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        rig.mod.channel.verified = True
        rig.mod.user._me = SimpleNamespace(first_name="Глэдис", username="g")
        (rig.mem / "telegram.json").write_text('{"last_post": %f, "posts": []}' % time.time(), encoding="utf-8")
        summary, data = await asyncio.to_thread(rig.mod.snapshot)
    assert summary.startswith("канал: готов, аккаунт: подключён, последний пост: ") and "ещё не было" not in summary
    assert data["channel_ok"] is True and data["account_ok"] is True and data["last_post_ts"] > 0


async def test_state_words_for_account_problems(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        assert rig.mod.user.state_word() == "подключается"
        (rig.mem / "tg_user.session").unlink()
        assert rig.mod.user.state_word() == "нет сессии"
        (rig.mem / "tg_user.session").write_bytes(b"")
        rig.mod.user._problem = "связь прервана"
        assert rig.mod.user.state_word() == "ошибка связи"


async def test_state_is_sent_on_change_and_periodically(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "STATE_EVERY_S", 0.3)
    monkeypatch.setattr(app_mod, "STATE_POLL_S", 0.05)
    async with Rig(tmp_path, monkeypatch) as rig:
        task = asyncio.create_task(rig.mod.run_state())
        try:
            first = (await rig.core.wait_message("state")).body
            assert first["summary"].startswith("канал: не проверен") and first["data"]["channel_ok"] is False
            n = len(rig.core.messages_of("state"))
            await rig.core.wait_for(lambda: len(rig.core.messages_of("state")) > n)      # раз в период, даже без изменений
            rig.stub_model(monkeypatch, POST)
            n = len(rig.core.messages_of("state"))
            await rig.core.call_tool("post", {})
            await rig.core.wait_for(lambda: rig.core.messages_of("state")[-1].body["data"]["last_post_ts"] > 0)
            last = rig.core.messages_of("state")[-1].body
            assert last["summary"].startswith("канал: готов") and last["data"]["channel_ok"] is True
        finally:
            task.cancel()


async def test_state_says_disabled_and_core_keeps_it(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await rig.core.set_enabled(False)
        await rig.core.wait_for(lambda: not rig.client.enabled)
        summary, _ = await asyncio.to_thread(rig.mod.snapshot)
        assert summary.startswith("канал: выключен")
        await rig.mod.channel._on_private({"message_id": 1, "chat": {"id": 5, "type": "private"}, "text": "x",
                                           "from": {"id": 5, "first_name": "П"}})
        await asyncio.sleep(0.1)
        assert rig.core.events == []                                                      # выключенный модуль молчит
