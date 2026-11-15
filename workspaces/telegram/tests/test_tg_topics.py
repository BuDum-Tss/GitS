"""Темы супергрупп-форумов: режим у каждой темы, события и ответы по теме, панель с карточками и аватарками. Telethon подменён."""
import datetime
import asyncio
import json
from types import SimpleNamespace

import pytest
from common import ChatModes, split_target, valid_key
from test_tg_events import FakeEvent
from test_tg_panel import Dlg, Msg, act, blocks_of, cards, find, flat, install
from tg_rig import FakeTelethon, Rig

from gits_sdk import ToolError
from gits_sdk import protocol as P

CHAT = -100500
GROUP = dict(private=False, group=True, title="Клуб", chat_id=CHAT, sender_id=9, first="Петя")


class TopicTL(FakeTelethon):
    """Клиент с темами форума: запоминает kwargs отправки и отвечает на GetForumTopicsRequest."""

    def __init__(self):
        super().__init__()
        self.kw: list[dict] = []
        self.requests: list = []
        self.topics = [(1, "General", "общее", 0), (5, "ПРОГРЕСС", "сделала шаг", 2), (7, "Объявления", "важно", 0)]
        self.photos: dict = {}
        self.fail_photo = False
        self.downloads: list = []

    async def send_message(self, ent, text, **kw):
        self.kw.append(kw)
        self.sent.append((ent, text))

    async def __call__(self, request):
        self.requests.append(request)
        when = datetime.datetime(2026, 10, 10, 12, 0)
        topics = [SimpleNamespace(id=i, title=t, icon_color=0xFB6F5F, top_message=100 + i, unread_count=u, pinned=i == 1,
                                  closed=False, date=when) for i, t, _, u in self.topics]
        msgs = [SimpleNamespace(id=100 + i, text=last, out=i == 5, date=when) for i, _, last, _ in self.topics]
        return SimpleNamespace(topics=topics, messages=msgs, count=len(topics))

    async def get_messages(self, ent, ids=None, limit=None, reply_to=None):
        return getattr(self, "_msgs", {}).get(ids)

    async def download_profile_photo(self, entity, file=None, download_big=False):
        self.downloads.append(file)
        if getattr(self, "hang_photo", False):
            await asyncio.sleep(3600)                      # как скачивание через недоступный CDN-сервер: ни ответа, ни ошибки
        if self.fail_photo:
            raise OSError("нет сети")
        with open(file, "wb") as f:
            f.write(b"JPEG")
        return file


def topic_event(topic, **kw):
    """Входящее в теме форума (topic None: General без reply_to)."""
    ev = FakeEvent(**{**GROUP, **kw})
    ev._chat.forum = True
    if topic is not None:
        ev.message.reply_to = SimpleNamespace(forum_topic=True, reply_to_top_id=topic, reply_to_msg_id=topic + 1)
    return ev


def setup(rig, chat_mode=None, topic_modes=None):
    rig.net.tl = tl = TopicTL()
    rig.net.install(rig.mod)
    user = rig.mod.user
    user._client = tl
    user._me = SimpleNamespace(id=1, first_name="Глэдис", username="glados_acc")

    async def entity(client, chat):
        return SimpleNamespace(id=CHAT, forum=True, username=None)
    user._entity = entity
    if chat_mode:
        user.modes.set(CHAT, chat_mode)
    for topic, mode in (topic_modes or {}).items():
        user.modes.set(CHAT, mode, topic)
    return user, tl


# ── хранение и разбор ──

def test_topic_modes_are_stored_by_chat_and_topic_with_inheritance(tmp_path):
    modes = ChatModes(str(tmp_path))
    assert modes.get(CHAT, "reply", topic=5) == "reply"
    modes.set(CHAT, "read")
    assert modes.get(CHAT, "reply", topic=5) == "read" and modes.explicit(CHAT, 5) is None      # без своего: как в группе
    modes.set(CHAT, "reply", topic=5)
    modes.set(CHAT, "ignore", topic=7)
    assert modes.get(CHAT, "ignore", topic=5) == "reply" and modes.get(CHAT, "reply", topic=7) == "ignore"
    assert modes.get(CHAT, "reply", topic=9) == "read" and modes.get(CHAT, "reply") == "read"     # группа целиком
    assert modes.topics(CHAT) == {5: "reply", 7: "ignore"}
    assert json.loads((tmp_path / "tg_chat_modes.json").read_text(encoding="utf-8"))["chats"] == {
        str(CHAT): "read", f"{CHAT}#5": "reply", f"{CHAT}#7": "ignore"}
    modes.set(CHAT, None, topic=5)                                                              # «Как в группе»
    assert modes.explicit(CHAT, 5) is None and modes.get(CHAT, "reply", topic=5) == "read"
    with pytest.raises(ValueError):
        modes.set(CHAT, None)                                                                   # у группы «как в группе» нет
    (tmp_path / "tg_chat_modes.json").write_text('{"chats": {"5#x": "read", "5#2": "read", "7": "write"}}', encoding="utf-8")
    assert modes.all() == {"5#2": "read"}


def test_target_parsing_and_keys():
    assert split_target("Клуб / ПРОГРЕСС") == ("Клуб", "ПРОГРЕСС") and split_target("Клуб › Тема") == ("Клуб", "Тема")
    assert split_target("-100500#5") == ("-100500", "5") and split_target("Клуб#5") == ("Клуб", "5")
    assert split_target("A/B") == ("A/B", "") and split_target("Клуб#тема") == ("Клуб#тема", "") and split_target("Маша") == ("Маша", "")
    assert valid_key("-1#5") and valid_key("7") and not valid_key("1#") and not valid_key("x#1")


# ── входящие ──

async def test_topic_message_event_has_topic_data_text_and_reply_target(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig)
        await user._on_incoming(topic_event(5, text="@glados_acc как успехи?", msg_id=11, mentioned=True, client=tl))
        ev = (await rig.core.wait_message("event")).body
        inbox = json.loads((rig.mem / "tg_inbox.json").read_text(encoding="utf-8"))
        act_ = user.dialogs.activity(CHAT, 5)
    assert ev["text"] == "Сообщение от Петя в группе «Клуб», теме «ПРОГРЕСС»: «@glados_acc как успехи?»"
    assert ev["data"]["topic_id"] == 5 and ev["data"]["topic_title"] == "ПРОГРЕСС" and ev["data"]["chat_id"] == CHAT
    assert ev["reply_channel"] == {"module": "telegram", "target": f"user:{CHAT}#5"} and ev["addressed_to_me"] is True
    assert inbox[-1]["chat"] == "Клуб › ПРОГРЕСС" and inbox[-1]["topic_id"] == 5
    assert act_["last_in"] > 0 and user.dialogs.activity(CHAT, 7)["last_in"] == 0 and user.dialogs.activity(CHAT)["last_in"] == 0
    assert len(tl.requests) == 1                                                                 # название темы: один запрос


async def test_general_topic_is_id_1_and_plain_group_has_no_topic(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig)
        await user._on_incoming(topic_event(None, text="в общем", msg_id=1, client=tl))
        general = (await rig.core.wait_message("event")).body
        await user._on_incoming(FakeEvent(text="@glados_acc ау", msg_id=2, mentioned=True, client=tl, **{**GROUP, "chat_id": -77}))
        await rig.core.wait_for(lambda: len(rig.core.events) >= 2)
        plain = rig.core.events[-1]
    assert general["data"]["topic_id"] == 1 and general["data"]["topic_title"] == "General"
    assert general["reply_channel"]["target"] == f"user:{CHAT}#1"
    assert "topic_id" not in plain["data"] and plain["reply_channel"]["target"] == "user:-77"


async def test_topic_mode_beats_group_mode_in_both_directions(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig, "reply", {7: "ignore", 5: "read"})
        await user._on_incoming(topic_event(7, text="@glados_acc тише", msg_id=1, mentioned=True, client=tl))      # тема в игноре
        await user._on_incoming(topic_event(5, text="@glados_acc читай", msg_id=2, mentioned=True, client=tl))     # тема только читать
        await user._on_incoming(topic_event(1, text="@glados_acc а тут", msg_id=3, mentioned=True, client=tl))     # как в группе: reply
        await rig.core.wait_for(lambda: len(rig.core.events) >= 2)
        evs = rig.core.events
    assert [e["data"]["topic_id"] for e in evs] == [5, 1]                                          # из игнорируемой темы события нет
    assert evs[0]["addressed_to_me"] is False and "reply_channel" not in evs[0] and evs[0]["data"]["chat_mode"] == "read"
    assert evs[1]["addressed_to_me"] is True and evs[1]["reply_channel"]["target"] == f"user:{CHAT}#1"
    inbox = json.loads((rig.mem / "tg_inbox.json").read_text(encoding="utf-8"))
    assert [r["topic_id"] for r in inbox] == [5, 1]


async def test_group_ignore_applies_to_topics_without_own_mode_only(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig, "ignore", {5: "reply"})
        await user._on_incoming(topic_event(7, text="@glados_acc 1", msg_id=1, mentioned=True, client=tl))
        await user._on_incoming(topic_event(5, text="@glados_acc 2", msg_id=2, mentioned=True, client=tl))
        await rig.core.wait_for(lambda: len(rig.core.events) >= 1)
        evs = rig.core.events
    assert [e["data"]["topic_id"] for e in evs] == [5]


async def test_continuation_is_counted_per_topic(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig)
        await user._on_incoming(topic_event(5, text="@glados_acc привет", msg_id=1, mentioned=True, client=tl))
        await user._sent(CHAT, 5)                                                                   # она ответила в теме 5
        assert user.dialogs.in_dialog(CHAT, 9, 900, 5) and not user.dialogs.in_dialog(CHAT, 9, 900, 7)
        await user._on_incoming(topic_event(5, text="а ещё?", msg_id=2, client=tl))
        await user._on_incoming(topic_event(7, text="и тут?", msg_id=3, client=tl))
        await rig.core.wait_for(lambda: len(rig.core.events) >= 3)
        evs = rig.core.events
    assert evs[1]["addressed_to_me"] is True and evs[1]["data"].get("continuation") is True       # та же тема
    assert evs[2]["addressed_to_me"] is False and "continuation" not in evs[2]["data"]            # другая тема: продолжения нет


# ── отправка ──

async def test_deliver_to_topic_replies_into_it_and_checks_the_topic_mode(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig, "read", {5: "reply"})
        assert await rig.core.request("deliver", {"target": f"user:{CHAT}#5", "text": "в тему"}) == {"delivered": True}
        assert tl.sent[-1][1] == "в тему" and tl.kw[-1] == {"reply_to": 5}
        assert user.dialogs.activity(CHAT, 5)["hour"] == 1 and user.dialogs.activity(CHAT)["hour"] == 0
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": f"user:{CHAT}#7", "text": "а сюда нельзя"})      # у темы режим группы: read
        assert e.value.code == "denied" and "только для чтения" in e.value.message
        with pytest.raises(ToolError):
            await rig.core.request("deliver", {"target": f"user:{CHAT}", "text": "в группу целиком"})
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": f"user:{CHAT}#x", "text": "?"})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError):
            await rig.core.request("deliver", {"target": f"bot:{CHAT}#5", "text": "бот темы не знает"})
        user.modes.set(CHAT, "reply")
        user.modes.set(CHAT, "ignore", 5)
        with pytest.raises(ToolError):
            await rig.core.request("deliver", {"target": f"user:{CHAT}#5", "text": "тема в игноре"})
        assert [t for _, t in tl.sent] == ["в тему"]
        user.modes.set(CHAT, "reply", 1)
        await rig.core.request("deliver", {"target": f"user:{CHAT}#1", "text": "в General"})
        assert tl.kw[-1] == {}                                                                       # General: без reply_to


async def test_message_tool_takes_chat_and_topic_by_name_or_id(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig, "read", {5: "reply"})
        user.known.add("Клуб")
        await rig.core.call_tool("message", {"to": "Клуб / ПРОГРЕСС", "text": "по названию"})
        assert tl.kw[-1] == {"reply_to": 5}
        await rig.core.call_tool("message", {"to": f"{CHAT}#5", "text": "по id"})
        assert tl.kw[-1] == {"reply_to": 5}
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Клуб / Объявления", "text": "нельзя"})       # режим группы: read
        assert e.value.code == "denied"
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Клуб", "text": "в General (как в группе: read)"})
        assert e.value.code == "denied"
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Клуб / Нет такой", "text": "x"})
        assert e.value.code == "bad_request" and "ПРОГРЕСС" in e.value.message
        tl._msgs = {50: SimpleNamespace(reply_to=SimpleNamespace(forum_topic=True, reply_to_top_id=7, reply_to_msg_id=50))}
        with pytest.raises(ToolError) as e:                                                          # reply_to из закрытой темы
            await rig.core.call_tool("message", {"to": "Клуб / ПРОГРЕСС", "text": "x", "reply_to": 50})
        assert e.value.code == "bad_request"
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Клуб", "text": "в закрытую тему ответом", "reply_to": 50})
        assert e.value.code == "denied"                                                              # тема из reply_to: 7, режим read
        assert [t for _, t in tl.sent] == ["по названию", "по id"]


async def test_message_tool_topic_in_a_plain_group_is_an_error_and_known_check_uses_the_group(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig)

        async def plain(client, chat):
            return SimpleNamespace(id=-9, forum=False, username=None)
        user._entity = plain
        user.known.add("Соседи")
        assert await rig.mod.message_check({"to": "Соседи / Тема", "text": "привет"}) is None      # группа известна
        assert "Чужие" in (await rig.mod.message_check({"to": "Чужие / Тема", "text": "привет"}) or "Чужие")
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "Соседи / Тема", "text": "x"})
        assert e.value.code == "bad_request" and "не форум" in e.value.message


async def test_inbox_reads_one_topic_and_hides_ignored_topics_in_the_whole_forum(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user, tl = setup(rig, None, {7: "ignore"})

        def m(i, topic, text):
            head = SimpleNamespace(forum_topic=True, reply_to_top_id=topic, reply_to_msg_id=topic)
            return SimpleNamespace(id=i, text=text, out=False, reply_to=head, date=datetime.datetime(2026, 10, 10, 12, 0),
                                   get_sender=_sender)

        async def _sender():
            return SimpleNamespace(first_name="Петя")
        calls = []

        async def get_messages(ent, limit=None, reply_to=None, ids=None):
            calls.append(reply_to)
            return [m(2, 7, "из игнора"), m(1, 5, "из прогресса")]
        tl.get_messages = get_messages
        whole = await rig.core.call_tool("inbox", {"chat": "Клуб"})
        assert "из прогресса" in whole and "из игнора" not in whole
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("inbox", {"chat": "Клуб / Объявления"})
        assert e.value.code == "denied"
        one = await rig.core.call_tool("inbox", {"chat": "Клуб / ПРОГРЕСС"})
        assert calls[-1] == 5 and "из прогресса" in one and "ТВОЯ АКТИВНОСТЬ" in one


# ── панель ──

def forum_install(rig, **kw):
    install(rig, [Dlg(5, "Маша", "user", 1, "привет"), Dlg(CHAT, "Клуб", "super", 4, "всем привет", forum=True, photo=777),
                  Dlg(-3, "Соседи", "group", 0, "ау")], kw.get("messages"))
    rig.net.tl.__class__ = TopicTL
    tl = rig.net.tl
    TopicTL.__init__(tl)
    msgs = kw.get("messages") or []

    async def iter_messages(ent, limit=50, reply_to=None):
        for m in list(reversed(msgs))[:limit]:
            yield m
    tl.iter_messages = iter_messages

    async def entity(client, chat):
        return SimpleNamespace(id=int(chat), forum=int(chat) == CHAT, username=None)
    rig.mod.user._entity = entity
    return tl


async def test_forum_card_expands_into_topic_cards_with_inherit_select(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        tl = forum_install(rig)
        rig.mod.user.modes.set(CHAT, "read")
        rig.mod.user.modes.set(CHAT, "reply", 5)
        blocks = await blocks_of(rig)
        club = cards(blocks)["Клуб"]
        assert club["tag"] == "темы: 3" and club["action"] == "toggle_topics" and club["expanded"] is False
        assert "children" not in club and club["select"]["value"] == "read" and club["badge"] == "4"
        assert not [r for r in tl.requests if r.limit != 1]                                       # темы не читаются, пока свёрнуто
        assert (await act(rig, "toggle_topics", {"chat": CHAT}))["ok"] is True
        club = cards(await blocks_of(rig))["Клуб"]
        assert club["expanded"] is True
        kids = {c["title"]: c for c in club["children"]}
        assert set(kids) == {"General", "ПРОГРЕСС", "Объявления"} and [c["title"] for c in club["children"]][0] == "General"   # закреплённая первой
        progress, news = kids["ПРОГРЕСС"], kids["Объявления"]
        assert progress["action"] == "open_chat" and progress["args"] == {"chat": CHAT, "topic": 5} and progress["badge"] == "2"
        assert progress["subtitle"] == "Вы: сделала шаг" and progress["color"] == "#FB6F5F" and kids["General"]["icon"] == "#"
        assert progress["select"]["value"] == "reply" and news["select"]["value"] == "inherit"
        assert news["select"]["options"][0] == {"value": "inherit", "label": "Как в группе (чтение)"}
        assert news["select"]["args"] == {"chat": CHAT, "topic": 7} and news["select"]["arg"] == "mode"
        assert (await act(rig, "toggle_topics", {"chat": CHAT}))["ok"] is True
        assert "children" not in cards(await blocks_of(rig))["Клуб"]


async def test_topic_set_mode_inherit_and_open_topic_header_and_send(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        tl = forum_install(rig, messages=[Msg(8, "в теме")])
        user = rig.mod.user
        user.modes.set(CHAT, "read")
        assert (await act(rig, "set_mode", {"chat": CHAT, "topic": 7, "mode": "ignore"}))["ok"] is True
        assert user.modes.explicit(CHAT, 7) == "ignore" and user.modes.explicit(CHAT) == "read"
        assert (await act(rig, "set_mode", {"chat": CHAT, "topic": 7, "mode": "inherit"}))["ok"] is True
        assert user.modes.explicit(CHAT, 7) is None
        assert (await act(rig, "set_mode", {"chat": CHAT, "mode": "inherit"}))["ok"] is False        # у группы такого пункта нет
        assert (await act(rig, "set_mode", {"chat": CHAT, "topic": 0, "mode": "read"}))["ok"] is False
        assert (await act(rig, "set_mode", {"chat": CHAT, "topic": 5, "mode": "write"}))["ok"] is False
        await act(rig, "set_mode", {"chat": CHAT, "topic": 5, "mode": "reply"})
        await act(rig, "toggle_topics", {"chat": CHAT})
        assert (await act(rig, "open_chat", {"chat": CHAT, "topic": 5}))["ok"] is True
        blocks = await blocks_of(rig)
        head = find(blocks, "cards")[-1]["items"][0]
        assert head["title"] == "Клуб › ПРОГРЕСС" and head["select"]["value"] == "reply" and head["select"]["args"] == {"chat": CHAT, "topic": 5}
        assert head["select"]["options"][0]["label"] == "Как в группе (чтение)"
        sel = [c for c in cards(blocks)["Клуб"]["children"] if c["title"] == "ПРОГРЕСС"][0]
        assert sel["selected"] is True and cards(blocks)["Клуб"].get("selected") in (None, False)
        form = find(blocks, "input")[-1]
        assert form["args"] == {"chat": CHAT, "topic": 5} and form["label"] == "Написать от лица агента"
        assert find(blocks, "table")[0]["rows"][0]["cells"][0] == 8
        assert (await act(rig, "send_message", {"chat": CHAT, "topic": 5, "value": "привет в тему"}))["ok"] is True
        assert tl.sent[-1][1] == "привет в тему" and tl.kw[-1] == {"reply_to": 5}
        user.modes.set(CHAT, "ignore", 5)                                                             # владелец пишет и в игноре
        assert (await act(rig, "send_message", {"chat": CHAT, "topic": 5, "value": "и тут"}))["ok"] is True
        async def last_in_topic(ent, ids=None, limit=None, reply_to=None):
            return [SimpleNamespace(id=60)]
        tl.get_messages = last_in_topic
        assert (await act(rig, "mark_read", {"chat": CHAT, "topic": 5}))["ok"] is True
        assert any(type(r).__name__ == "ReadDiscussionRequest" for r in tl.requests)
        assert (await act(rig, "open_chat", {"chat": CHAT}))["ok"] is True and rig.mod.panel.topic == 1   # форум без темы: General
        await act(rig, "close_chat")
        assert rig.mod.panel.topic is None


async def test_forum_panel_with_many_topics_and_chats_fits_the_limit(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        dialogs = [Dlg(-1000 - i, f"Форум {i} " + "я" * 60, "super", 3, "последнее " + "я" * 150, forum=True) for i in range(50)]
        install(rig, dialogs, [Msg(i, "я" * 400) for i in range(1, 120)])
        tl = rig.net.tl
        tl.__class__ = TopicTL
        TopicTL.__init__(tl)
        tl.topics = [(i, f"Тема {i} " + "ы" * 50, "текст " + "я" * 150, 1) for i in range(1, 101)]
        for d in dialogs[:6]:
            await act(rig, "toggle_topics", {"chat": d.id})
        blocks = await blocks_of(rig)
        assert len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()) <= P.PANEL_SET_MAX_BYTES
        first = find(blocks, "cards")[0]["items"][0]
        assert len(first["children"]) <= P.PANEL_CARDS_MAX
        await act(rig, "open_chat", {"chat": dialogs[0].id, "topic": 5})
        for _ in range(3):
            await act(rig, "history_more", {"chat": dialogs[0].id})
        blocks = await blocks_of(rig)
        assert len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()) <= P.PANEL_SET_MAX_BYTES


# ── аватарки ──

async def test_avatars_are_downloaded_lazily_cached_and_named_from_ids_only(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        tl = forum_install(rig)
        blocks = await blocks_of(rig)
        club = cards(blocks)["Клуб"]
        assert club["avatar"] == "av_m100500_777.jpg" and "avatar" not in cards(blocks)["Маша"]      # у Маши фото нет
        assert (rig.mem / "tg_avatars" / "av_m100500_777.jpg").read_bytes() == b"JPEG" and len(tl.downloads) == 1
        assert not list((rig.mem / "tg_avatars").glob("*.tmp"))
        await blocks_of(rig)
        assert len(tl.downloads) == 1                                                                 # из кэша на диске
        head_chat = rig.mod.user.avatar_name(CHAT, 777)
        assert head_chat == "av_m100500_777.jpg" and rig.mod.user.avatar_name("../x", 1) is None
        assert rig.mod.user.avatar_name(5, None) is None and rig.mod.user.avatar_name(5, True) is None


async def test_avatar_change_replaces_the_old_file_and_failure_means_a_letter(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        tl = forum_install(rig)
        await blocks_of(rig)
        club = [d for d in rig.mod.panel._dialogs if d["id"] == CHAT][0]
        club["entity"].photo.photo_id = 888                                                           # сменили фото
        assert cards(await blocks_of(rig))["Клуб"]["avatar"] == "av_m100500_888.jpg"
        assert sorted(p.name for p in (rig.mem / "tg_avatars").iterdir()) == ["av_m100500_888.jpg"]   # старое удалено
        club["entity"].photo.photo_id = 999
        tl.fail_photo = True
        assert "avatar" not in cards(await blocks_of(rig))["Клуб"]                                    # ошибка: буква-заглушка
        n = len(tl.downloads)
        await blocks_of(rig)
        assert len(tl.downloads) == n                                                                 # сразу повторно не мучаем
        assert not list((rig.mem / "tg_avatars").glob("*.tmp"))


async def test_no_more_than_20_new_avatars_per_refresh(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(i, f"Чат {i}", "user", photo=1000 + i) for i in range(1, 41)])
        rig.net.tl.__class__ = TopicTL
        tl = rig.net.tl
        TopicTL.__init__(tl)
        first = cards(await blocks_of(rig))
        assert len(tl.downloads) == 20 and sum(1 for c in first.values() if "avatar" in c) == 20
        second = cards(await blocks_of(rig))
        assert len(tl.downloads) == 40 and all("avatar" in c for c in second.values())


async def test_hung_avatar_download_times_out_and_does_not_freeze_the_panel(tmp_path, monkeypatch):
    import telegram_user
    monkeypatch.setattr(telegram_user, "AVATAR_TIMEOUT_S", 0.05)
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(i, f"Чат {i}", "user", photo=1000 + i) for i in range(1, 41)])
        rig.net.tl.__class__ = TopicTL
        tl = rig.net.tl
        TopicTL.__init__(tl)
        tl.hang_photo = True
        for k in range(1, 4):                                                       # каждое обновление: одна зависшая попытка, не 20
            first = cards(await asyncio.wait_for(blocks_of(rig), 5))
            assert len(tl.downloads) == k and not any("avatar" in c for c in first.values())
        await asyncio.wait_for(blocks_of(rig), 5)
        assert len(tl.downloads) == 3                                               # три таймаута подряд: аватарки выключены
        assert not list((rig.mem / "tg_avatars").glob("*.tmp"))


def test_avatar_dir_is_declared_in_serve_dirs_next_to_the_qr_dir():
    from manifest import build_manifest
    assert P.validate_manifest(build_manifest(["C:\\mem\\tg_login", "C:\\mem\\tg_avatars"])) == []
    assert build_manifest(["a", "b"])["panel"]["serve_dirs"] == ["a", "b"]
    assert "тем" in build_manifest()["tools"][0]["description"] and len(build_manifest()["tools"][0]["description"]) <= 300
    assert flat([])  == []
