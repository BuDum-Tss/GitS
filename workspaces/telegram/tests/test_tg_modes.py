"""Режимы чатов её аккаунта (ignore, read, reply): жёстко в коде слушателя и отправки, а не в подсказках. Telethon подменён."""
import json
from types import SimpleNamespace

import pytest
from common import ChatModes, valid_mode
from test_tg_events import FakeEvent
from tg_rig import Rig

from gits_sdk import ToolError

GROUP = dict(private=False, group=True, title="Соседи", chat_id=-100, sender_id=9, first="Петя")
OWNER = 777


async def events(rig, n):
    await rig.core.wait_for(lambda: len(rig.core.events) >= n)
    return rig.core.events


def setup_user(rig, mode=None, chat=-100):
    user = rig.mod.user
    user._client = rig.tl
    user._me = SimpleNamespace(id=1, first_name="Глэдис", username="glados_acc")
    if mode:
        user.modes.set(chat, mode)
    return user


async def fixed_entity(rig, chat=-100):
    async def entity(client, name):
        return ("chat", chat)
    rig.mod.user._entity = entity


# ── хранение ──

def test_chat_modes_file_is_atomic_and_keeps_only_ids_and_modes(tmp_path):
    modes = ChatModes(str(tmp_path))
    assert modes.get(5) == "reply" and modes.get(5, "read") == "read" and modes.explicit(5) is None
    assert modes.set(5, "ignore") and modes.set(-100123, "read")
    assert modes.get(5, "reply") == "ignore" and modes.explicit(-100123) == "read"
    assert json.loads((tmp_path / "tg_chat_modes.json").read_text(encoding="utf-8")) == {
        "chats": {"5": "ignore", "-100123": "read"}}
    assert not list(tmp_path.glob("*.tmp"))                                   # временный файл заменён, не оставлен
    with pytest.raises(ValueError):
        modes.set(5, "write")
    (tmp_path / "tg_chat_modes.json").write_text('{"chats": {"7": "write", "x": "read", "8": "read"}}', encoding="utf-8")
    assert modes.all() == {"8": "read"}                                       # мусор отбрасывается
    (tmp_path / "tg_chat_modes.json").write_text("битый json", encoding="utf-8")
    assert modes.all() == {} and valid_mode("что-то") == "reply"


# ── слушатель: ignore ──

async def test_ignore_sends_nothing_to_the_core_and_touches_nothing(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "ignore")
        await user._on_incoming(FakeEvent(text="@glados_acc ты тут?", msg_id=1, mentioned=True, client=rig.tl, **GROUP))
        await user._on_incoming(FakeEvent(text="ГЛаДОС, ответь", msg_id=2, reply_out=True, **GROUP))
        await rig.mod.user._on_incoming(FakeEvent(text="это событие дойдёт", msg_id=3, chat_id=5, sender_id=5))
        evs = await events(rig, 1)
    assert [e["text"] for e in evs] == ["Сообщение от Маша в её личке: «это событие дойдёт»"]      # из группы ни одного
    assert rig.tl.read_acks == [] or all(a[0] != -100 for a in rig.tl.read_acks)
    inbox = json.loads((rig.mem / "tg_inbox.json").read_text(encoding="utf-8"))
    assert [r["chat_id"] for r in inbox] == [5]                                              # в inbox игнорируемый чат не пишется
    assert not user.known.has("Соседи")                                                       # чат не «запомнен»
    dialogs = (rig.mem / "tg_dialogs.json")
    assert not dialogs.exists() or "-100" not in json.loads(dialogs.read_text(encoding="utf-8"))["chats"]


# ── слушатель: read ──

async def test_read_mode_group_tag_does_not_make_reply_mandatory(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "read")
        await user._on_incoming(FakeEvent(text="@glados_acc ау", msg_id=1, mentioned=True, client=rig.tl, **GROUP))
        await user._on_incoming(FakeEvent(text="ответ ей", msg_id=2, reply_out=True, client=rig.tl, **GROUP))
        await user._on_incoming(FakeEvent(text="ГЛаДОС, привет", msg_id=3, client=rig.tl, **GROUP))
        evs = await events(rig, 3)
    for e in evs:
        assert e["kind"] == "telegram.group" and e["addressed_to_me"] is False          # режим сильнее R6
        assert "reply_channel" not in e                                                 # отвечать некуда
        assert e["text"].endswith("[чат только для чтения: писать сюда нельзя]")
        assert e["data"]["chat_mode"] == "read"
    assert [a for a in rig.tl.read_acks if a[0] == -100] == []                          # «прочитано» не отправляется


async def test_read_mode_private_chat_is_not_addressed_but_stays_memorable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "read", chat=555)
        await user._on_incoming(FakeEvent(text="привет", chat_id=555, sender_id=555, client=rig.tl))
        ev = (await events(rig, 1))[0]
    assert ev["kind"] == "telegram.dm" and ev["addressed_to_me"] is False and ev["memorable"] is True
    assert "reply_channel" not in ev and "писать сюда нельзя" in ev["text"] and rig.tl.read_acks == []
    assert ev["sender"]["id"] == "tg:555"


async def test_reply_mode_is_unchanged(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "reply")
        await user._on_incoming(FakeEvent(text="@glados_acc ау", msg_id=1, mentioned=True, client=rig.tl, **GROUP))
        ev = (await events(rig, 1))[0]
    assert ev["addressed_to_me"] is True and ev["reply_channel"] == {"module": "telegram", "target": "user:-100", "tool": "telegram.text"}
    assert "писать сюда нельзя" not in ev["text"] and "chat_mode" not in ev["data"]


async def test_default_chat_mode_setting_applies_to_new_chats(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"default_chat_mode": "read"}) as rig:
        user = setup_user(rig)
        assert user.mode_of(-100) == "read" and user.modes.explicit(-100) is None
        user.modes.set(-100, "reply")                                                   # явный выбор сильнее значения по умолчанию
        assert user.mode_of(-100) == "reply"
    async with Rig(tmp_path / "b", monkeypatch, telethon=True, module_settings={"default_chat_mode": "ерунда"}) as rig:
        assert rig.mod.user.mode_of(-100) == "reply"                                   # незнакомое значение: reply


async def test_owner_private_chat_is_always_reply(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER,
                                                                         "default_chat_mode": "ignore"}) as rig:
        user = setup_user(rig, "ignore", chat=OWNER)
        assert user.mode_of(OWNER) == "reply" and user.mode_of(555) == "ignore"
        await user._on_incoming(FakeEvent(text="ты скоро?", sender_id=OWNER, chat_id=OWNER, first="Аня", client=rig.tl))
        ev = (await events(rig, 1))[0]
        assert ev["addressed_to_me"] is True and ev["sender"]["is_owner"] is True
        await fixed_entity(rig, OWNER)
        await user.send("Аня", "иду")                                                   # её аккаунт пишет в личку владельца
        assert rig.tl.sent[-1][1] == "иду"


# ── отправка: любой путь ──

@pytest.mark.parametrize("mode,words", [("read", "только для чтения"), ("ignore", "игнор")])
async def test_message_tool_refuses_in_read_and_ignore(tmp_path, monkeypatch, mode, words):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER}) as rig:
        setup_user(rig, mode)
        await fixed_entity(rig)
        rig.mod.user.known.add("Соседи")                                               # из чата уже писали: спрашивать не надо
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Соседи", "text": "привет всем"})
        assert e.value.code == "denied" and words in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Соседи", "text": "ответ", "reply_to": 5})
        assert e.value.code == "denied"
        assert rig.tl.sent == [] and rig.tl.files == []


async def test_files_and_deliver_and_reply_are_closed_too(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "read")
        await fixed_entity(rig)
        (rig.mem / "screenshots").mkdir()
        (rig.mem / "screenshots" / "shot_1.png").write_bytes(b"png")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Соседи", "text": "смотри", "files": ["shot_1.png"]})
        assert e.value.code == "denied" and "только для чтения" in e.value.message
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": "user:-100", "text": "ответ"})        # путь ядра: ответ на событие
        assert e.value.code == "denied" and "только для чтения" in e.value.message
        with pytest.raises(Exception, match="только для чтения"):
            await user.reply(-100, "прямой вызов")
        user.modes.set(-100, "ignore")
        with pytest.raises(ToolError):
            await rig.core.request("deliver", {"target": "user:-100", "text": "ответ"})
        assert rig.tl.sent == [] and rig.tl.files == []
        user.modes.set(-100, "reply")
        assert await rig.core.request("deliver", {"target": "user:-100", "text": "теперь можно"}) == {"delivered": True}
        assert rig.tl.sent[-1][1] == "теперь можно"


async def test_owner_manual_send_ignores_modes_but_agent_send_does_not(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig, "read")
        await fixed_entity(rig)
        with pytest.raises(Exception, match="только для чтения"):
            await user.send("Соседи", "от неё")
        await user.send("Соседи", "от владельца", by_owner=True)
        user.modes.set(-100, "ignore")
        await user.send("Соседи", "от владельца в игноре", by_owner=True)
        assert [t for _, t in rig.tl.sent] == ["от владельца", "от владельца в игноре"]
        assert rig.mod.user.dialogs.activity(-100)["hour"] == 2                      # попало в её активность как её сообщение


# ── inbox ──

async def test_inbox_marks_ignored_and_read_only_chats_and_hides_ignored_content(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = setup_user(rig)
        user.modes.set(-100, "ignore")
        user.modes.set(-200, "read")
        user._append_inbox({"ts": 1e9, "chat": "Соседи", "chat_id": -100, "from": "Петя", "text": "старое из игнора", "private": False})
        user._append_inbox({"ts": 1e9, "chat": "Читальня", "chat_id": -200, "from": "Вася", "text": "читаемое", "private": False})

        class Dialog:
            def __init__(self, name, ident):
                self.name, self.id, self.is_user, self.is_group, self.is_channel, self.unread_count = name, ident, False, True, False, 3

        async def iter_dialogs(limit=20):
            for d in (Dialog("Соседи", -100), Dialog("Читальня", -200), Dialog("Болталка", -300)):
                yield d

        rig.tl.iter_dialogs = iter_dialogs
        listing = await rig.core.call_tool("inbox", {})
        assert "Соседи (группа, непрочитано 3, id -100, ИГНОРИРУЕТСЯ" in listing
        assert "Читальня (группа, непрочитано 3, id -200, только чтение" in listing
        assert "Болталка (группа, непрочитано 3, id -300)" in listing
        await fixed_entity(rig)
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("inbox", {"chat": "Соседи"})
        assert e.value.code == "denied" and "игнор" in e.value.message
        unread = await rig.core.call_tool("inbox", {"unread": True})
        assert "читаемое" in unread and "старое из игнора" not in unread
        assert user.unread_count() == 0                                              # игнорируемое в счётчик не входит


# ── ядро: режим сильнее R6 ──

async def test_real_core_r6_fires_in_reply_mode_and_not_in_read_mode(tmp_path, monkeypatch):
    from test_tg_with_real_core import stack
    async with stack(tmp_path, monkeypatch) as s:
        user = s.mod.user
        user._me = SimpleNamespace(id=1, first_name="Глэдис", username="glados_acc")
        user.modes.set(-100, "read")
        await user._on_incoming(FakeEvent(text="@glados_acc ты тут?", msg_id=1, mentioned=True, **GROUP))
        import asyncio
        await asyncio.sleep(0.5)
        assert not s.core.io.inputs                                                  # R6 не сработало: разговора нет
        user.modes.set(-100, "reply")
        await user._on_incoming(FakeEvent(text="@glados_acc ты тут?", msg_id=2, mentioned=True, **GROUP))
        await s.core.wait_for(lambda: s.core.io.inputs) if hasattr(s.core, "wait_for") else await asyncio.sleep(0.5)
        assert s.core.io.inputs
