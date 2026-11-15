"""Инструмент message: адресаты, файлы, разрешение по адресату, приватность на каждом пути; inbox; владелец по @имени."""
import json

import pytest
from tg_rig import Rig

from gits_sdk import ToolError

OWNER = 777


def make_shot(rig, name="shot_20261010_120000_screen.png", data=b"png"):
    d = rig.mem / "screenshots"
    d.mkdir(exist_ok=True)
    (d / name).write_bytes(data)
    return name


def make_resource(tmp_path, monkeypatch, rel, data=b"data"):
    """Файл персонажа, доступный как идентификатор core.resources (папка тома персонажа)."""
    monkeypatch.setenv("CHARACTER_HOME", str(tmp_path))
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


async def fake_entity(rig):
    async def entity(client, chat):
        return ("chat", chat)
    rig.mod.user._entity = entity


# ── разрешение по адресату ──

async def test_check_by_address_channel_owner_known_and_unknown(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER}) as rig:
        check = rig.core.check_tool
        assert await check("message", {"text": "привет"}) is None                          # канал по умолчанию
        assert await check("message", {"to": "channel", "text": "привет"}) is None
        assert await check("message", {"to": "owner", "text": "привет"}) is None
        assert await check("message", {"to": str(OWNER), "text": "привет"}) is None
        unknown = await check("message", {"to": "Маша", "text": "привет"})
        assert unknown and "ещё не писали" in unknown                                      # незнакомый чат: вопрос
        rig.mod.user.known.add(555, "Маша", "masha_x")
        for who in ("Маша", "маша", "@masha_x", "555"):                                    # писала в её аккаунт: без вопроса
            assert await check("message", {"to": who, "text": "привет"}) is None, who
        assert await check("message", {"to": "Паша", "text": "привет"}) is not None        # другой: вопрос


async def test_check_known_chat_from_her_account_inbox(tmp_path, monkeypatch):
    from test_tg_events import FakeEvent

    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="привет", sender_id=9, chat_id=-100, private=False, group=True,
                                                  title="Соседи"))
        assert await rig.core.check_tool("message", {"to": "Соседи", "text": "всем привет"}) is None
        assert await rig.core.check_tool("message", {"to": "-100", "text": "всем привет"}) is None
        assert await rig.core.check_tool("message", {"to": "Другие", "text": "всем привет"}) is not None


async def test_check_private_words_raise_the_question_even_for_known_chat(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, terms=["Анна"]) as rig:
        rig.mod.user.known.add(5, "Маша")
        danger = await rig.core.check_tool("message", {"to": "Маша", "text": "Анна просила привет"})
        assert danger and "Анн" not in danger                                              # причина личное не повторяет


# ── отправка по адресатам и приватность на каждом пути ──

async def test_message_to_chat_from_her_account_and_private_words_blocked(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, terms=["Анна"]) as rig:
        await fake_entity(rig)
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Маша", "text": "Анна просила привет"})
        assert e.value.code == "denied" and rig.tl.sent == []
        assert await rig.core.call_tool_reply("message", {"to": "Маша", "text": "привет"}) == ("отправлено в «Маша»", ("user:Маша",))
        assert rig.tl.sent == [(("chat", "Маша"), "привет")]
        assert rig.api_calls("sendMessage") == []


async def test_message_to_owner_goes_from_the_bot(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER}) as rig:
        assert await rig.core.call_tool("message", {"to": "owner", "text": "Анна, привет"}) == "отправлено владельцу"
        assert rig.api_calls("sendMessage") == [{"chat_id": OWNER, "text": "Анна, привет"}]      # своё имя владельцу можно
        await rig.core.call_tool("message", {"to": "owner", "text": "ответ", "reply_to": 42})
        assert rig.api_calls("sendMessage")[-1] == {"chat_id": OWNER, "text": "ответ", "reply_to_message_id": 42}
        assert not (rig.mem / "telegram.json").exists()                                           # не пост в канал


async def test_message_to_owner_without_owner_is_unavailable(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "owner", "text": "привет"})
    assert e.value.code == "unavailable"


async def test_channel_text_filters_each_path(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, terms=["Борис"]) as rig:
        shot = make_shot(rig)
        for args in ({"text": "Борис снова проспал"}, {"text": "см. https://example.com"}, {"text": "Звоните +7 999 123 45 67"},
                     {"text": "Борису привет", "files": [shot]}):                                 # подпись к файлу тоже
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool("message", args)
            assert e.value.code == "denied", args
        assert rig.api_calls("sendMessage") == [] and rig.uploads == []


async def test_message_requires_text_topic_or_files(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {})
    assert e.value.code == "bad_request"


# ── файлы ──

async def test_one_photo_with_caption(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        name = make_shot(rig)
        assert "опубликован пост" in await rig.core.call_tool("message", {"text": "Вот снимок.", "files": ["last"]})
        (method, data, files), = rig.uploads
        assert method == "sendPhoto" and data == {"chat_id": "@chan", "caption": "Вот снимок."}
        assert files["photo"] == (name, b"png")
        assert rig.api_calls("sendMessage") == []
        assert rig.read_state()["last_post"] > 0


async def test_several_photos_go_as_album_and_by_name(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        a, b = make_shot(rig, "shot_1.png"), make_shot(rig, "shot_2.jpg", b"jpg")
        await rig.core.call_tool("message", {"text": "Два снимка.", "files": [a, b]})
        (method, data, files), = rig.uploads
        media = json.loads(data["media"])
        assert method == "sendMediaGroup" and media[0]["caption"] == "Два снимка." and "caption" not in media[1]
        assert set(files) == {"p0", "p1"}


async def test_video_voice_and_document_by_extension(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        video = make_resource(tmp_path, monkeypatch, "montage/out/final.mp4", b"mp4 fixture")
        voice = make_resource(tmp_path, monkeypatch, "shared/note.ogg", b"OggS")
        doc = make_resource(tmp_path, monkeypatch, "shared/report.pdf", b"%PDF")
        await rig.core.call_tool("message", {"files": ["montage/final.mp4"], "text": "Тест"})
        await rig.core.call_tool("message", {"files": [voice]})
        await rig.core.call_tool("message", {"files": [doc]})
        assert video and [(m, f) for m, _, f in rig.uploads] == [
            ("sendVideo", {"video": ("final.mp4", b"mp4 fixture")}), ("sendVoice", {"voice": ("note.ogg", b"OggS")}),
            ("sendDocument", {"document": ("report.pdf", b"%PDF")})]
        assert rig.uploads[0][1] == {"chat_id": "@chan", "caption": "Тест", "supports_streaming": "true"}


async def test_mixed_files_go_one_by_one_and_text_follows(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        shot = make_shot(rig)
        doc = make_resource(tmp_path, monkeypatch, "shared/report.pdf", b"%PDF")
        await rig.core.call_tool("message", {"files": [shot, doc], "text": "Материалы."})
        assert [m for m, _, _ in rig.uploads] == ["sendPhoto", "sendDocument"]
        assert all("caption" not in d for _, d, _ in rig.uploads)
        assert rig.api_calls("sendMessage") == [{"chat_id": "@chan", "text": "Материалы."}]


async def test_files_to_owner_and_to_chat_from_her_account(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER}) as rig:
        await fake_entity(rig)
        shot = make_shot(rig)
        doc = make_resource(tmp_path, monkeypatch, "shared/report.pdf", b"%PDF")
        await rig.core.call_tool("message", {"to": "owner", "files": [shot], "text": "Смотри."})
        assert rig.uploads[0][1]["chat_id"] == OWNER and rig.uploads[0][1]["caption"] == "Смотри."
        assert "файлов 1" in await rig.core.call_tool("message", {"to": "Маша", "files": [doc], "text": "Отчёт"})
        sent = rig.tl.files[0]
        assert sent["ent"] == ("chat", "Маша") and sent["data"] == b"%PDF" and sent["caption"] == "Отчёт"
        assert sent["force_document"] is True and sent["voice_note"] is False


async def test_file_errors_and_limits(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"files": ["нет-такого.png"]})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"files": "last"})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError):
            await rig.core.call_tool("message", {"files": [f"shot_{i}.png" for i in range(11)]})
        monkeypatch.setattr("telegram_module.FILE_MAX_BYTES", 2)
        make_shot(rig, data=b"toolong")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"files": ["last"]})
        assert "50 МБ" in e.value.message and rig.uploads == []


async def test_voice_text_to_owner_and_channel(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER}) as rig:
        await rig.core.call_tool("message", {"to": "owner", "text": "Привет вслух.", "voice": True})
        (method, data, files), = rig.uploads
        assert (method, data["chat_id"], data["caption"], files["voice"][1]) == ("sendVoice", OWNER, "Привет вслух.", b"OggS-voice")


# ── inbox ──

async def test_inbox_three_modes(tmp_path, monkeypatch):
    from test_tg_events import FakeEvent

    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        await rig.mod.user._on_incoming(FakeEvent(text="первое"))

        class Dialog:
            name, is_user, is_group, unread_count, id = "Маша", True, False, 1, 555

        class Msg:
            out, text = False, "привет из чата"

            def __init__(self):
                import datetime
                self.date = datetime.datetime(2026, 10, 10, 12, 0)

            async def get_sender(self):
                return type("S", (), {"first_name": "Маша"})()

        async def iter_dialogs(limit=20):
            yield Dialog()

        async def get_messages(ent, limit=15):
            return [Msg()]

        async def ack(ent):
            return None

        async def entity(client, chat):
            return ("chat", chat)

        rig.tl.iter_dialogs, rig.tl.get_messages, rig.tl.send_read_acknowledge = iter_dialogs, get_messages, ack
        rig.mod.user._entity = entity
        assert "Маша (личный, непрочитано 1, id 555)" in await rig.core.call_tool("inbox", {})              # список диалогов
        out = await rig.core.call_tool("inbox", {"chat": "Маша"})                                           # чат
        assert "ДАННЫЕ ИЗ ЧАТА" in out and "привет из чата" in out
        out = await rig.core.call_tool("inbox", {"unread": True})                                           # недавнее
        assert "первое" in out and rig.mod.user.unread_count() == 0


async def test_inbox_without_account_explains(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        for args in ({}, {"chat": "Маша"}):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool("inbox", args)
            assert e.value.code == "unavailable" and "TELEGRAM_API_ID" in e.value.message


# ── владелец: owner_tg_id, @имя, id ──

def msg(uid, username=None, first="Аня", chat=None, message_id=1):
    sender = {"id": uid, "first_name": first, "is_bot": False}
    if username:
        sender["username"] = username
    return {"message_id": message_id, "chat": {"id": chat or uid, "type": "private"}, "text": "привет", "from": sender}


@pytest.mark.parametrize("owner, uid, username, expected", [
    ("@Anya_Owner", 5, "anya_owner", True),            # по @username
    ("anya_owner", 5, "Anya_Owner", True),             # без @ и без регистра
    ("5", 5, None, True),                              # строка owner равна числовому id
    ("@anya_owner", 6, "other", False),
    ("@anya_owner", 6, None, False),                   # у отправителя нет username
    ("", 5, "anya_owner", False),                      # ничего не задано
])
async def test_owner_matches_by_username_or_id(tmp_path, monkeypatch, owner, uid, username, expected):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": owner}) as rig:
        assert rig.mod.channel.owner.is_owner(uid, username) is expected


async def test_old_owner_tg_id_still_works_next_to_owner_string(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner_tg_id": OWNER, "owner": "@someone"}) as rig:
        owner = rig.mod.channel.owner
        assert [owner.is_owner(OWNER), owner.is_owner(5, "someone"), owner.is_owner(6, "third")] == [True, True, False]


async def test_owner_id_is_learned_from_username_and_used_for_delivery(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": "@Anya_Owner"}) as rig:
        assert rig.mod.channel.owner_id() == 0
        with pytest.raises(ToolError):                                                       # id ещё не известен
            await rig.core.request("deliver", {"target": "owner", "text": "вопрос"})
        await rig.mod.channel._on_private(msg(5, "anya_owner"))
        assert rig.mod.channel.owner_id() == 5
        assert json.loads((rig.mem / "tg_owner.json").read_text(encoding="utf-8"))["owner_id_learned"] == 5
        assert await rig.core.request("deliver", {"target": "owner", "text": "вопрос"}) == {"delivered": True}
        assert rig.api_calls("sendMessage")[-1] == {"chat_id": 5, "text": "вопрос"}
        assert await rig.core.call_tool("message", {"to": "@anya_owner", "text": "и ещё"}) == "отправлено владельцу"
        assert rig.api_calls("sendMessage")[-1]["chat_id"] == 5


async def test_learned_id_survives_username_change_but_not_owner_change(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": "@anya_owner"}) as rig:
        await rig.mod.channel._on_private(msg(5, "anya_owner"))
        assert rig.mod.channel.owner.is_owner(5, "renamed") is True                           # сменила @имя, id тот же
        await rig.core.update_settings({}, module_settings={"owner": "@someone_else"})
        await rig.core.wait_for(lambda: rig.client.module_settings.get("owner") == "@someone_else")
        assert rig.mod.channel.owner_id() == 0                                               # выученный id принадлежал другому


async def test_numeric_owner_string_gives_owner_id(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": "123456"}) as rig:
        assert rig.mod.channel.owner_id() == 123456
        await rig.core.request("deliver", {"target": "owner", "text": "вопрос"})
        assert rig.api_calls("sendMessage")[-1]["chat_id"] == 123456


async def test_account_dm_uses_the_same_owner_rule(tmp_path, monkeypatch):
    from test_tg_events import FakeEvent

    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner": "@Anya_Owner"}) as rig:
        ev = FakeEvent(text="ты скоро?", sender_id=5, chat_id=5, first="Аня")
        ev._sender.username = "anya_owner"
        await rig.mod.user._on_incoming(ev)
        body = (await rig.core.wait_message("event")).body
    assert body["sender"]["is_owner"] is True
