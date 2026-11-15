"""Панель Telegram: вход, список диалогов с режимами, открытый чат, действия владельца. Telethon подменён, браузера нет."""
import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest
import telegram_module as app_mod
from tg_rig import Rig

from gits_sdk import ToolError
from gits_sdk import protocol as P

OWNER = 777


class Msg:
    def __init__(self, mid, text, out=False, reply=None, name="Маша", minutes=0):
        self.id, self.text, self.out, self.reply_to_msg_id, self.edit_date = mid, text, out, reply, None
        self.sender = SimpleNamespace(first_name=name, last_name=None, username=None)
        self.sender_id = 5
        self.date = datetime.datetime(2026, 10, 10, 12, minutes)


class Dlg:
    def __init__(self, ident, name, kind="user", unread=0, last="", forum=False, photo=None, out=False):
        self.id, self.name, self.unread_count = ident, name, unread
        self.is_user, self.is_group, self.is_channel = kind == "user", kind in ("group", "super"), kind in ("channel", "super")
        self.entity = SimpleNamespace(username=None, bot=False, megagroup=kind == "super", forum=forum,
                                      photo=SimpleNamespace(photo_id=photo) if photo else None)
        self.message = Msg(1, last, out=out) if last else None
        self.dialog = SimpleNamespace(read_outbox_max_id=10)


def install(rig, dialogs, messages=None):
    tl = rig.tl
    tl.deleted, tl.reads = [], []

    async def iter_dialogs(limit=200):
        for d in dialogs[:limit]:
            yield d

    async def iter_messages(ent, limit=50):
        for m in list(reversed(messages or []))[:limit]:
            yield m

    async def get_messages(ent, ids=None, limit=None):
        return next((m for m in messages or [] if m.id == ids), None)

    async def delete_messages(ent, ids):
        tl.deleted.append(ids)

    async def entity(client, chat):
        return ("chat", int(chat))

    async def ack(ent, **kw):
        tl.reads.append(ent)

    tl.iter_dialogs, tl.iter_messages, tl.get_messages, tl.delete_messages = iter_dialogs, iter_messages, get_messages, delete_messages
    tl.send_read_acknowledge = ack
    rig.mod.user._entity = entity
    rig.mod.user._me = SimpleNamespace(first_name="Глэдис", username="glados")


def flat(blocks):
    """Блоки панели с содержимым колонок split (сначала левая, потом правая)."""
    out = []
    for b in blocks:
        if b["type"] == "split":
            out += flat(b["left"]) + flat(b["right"])
        else:
            out.append(b)
    return out


def find(blocks, kind, **kw):
    return [b for b in flat(blocks) if b["type"] == kind and all(b.get(k) == v for k, v in kw.items())]


def cards(blocks):
    """Карточки диалогов (первый блок cards левой колонки) по названию."""
    return {c["title"]: c for c in find(blocks, "cards")[0]["items"]}


async def blocks_of(rig):
    rig.mod.panel.invalidate()
    blocks = await rig.mod.panel_blocks()
    actions = set(rig.client.manifest["panel"]["actions"])
    assert P.validate_panel_blocks(blocks, actions) == [], blocks
    return blocks


async def act(rig, action, args=None):
    return await rig.core.panel_action(action, args or {})


async def test_manifest_declares_all_panel_actions_and_default_mode_setting():
    from manifest import build_manifest
    from telegram_panel import ACTIONS
    m = build_manifest(["C:\\x"])
    assert m["panel"]["actions"] == list(ACTIONS) and m["panel"]["serve_dirs"] == ["C:\\x"]
    assert m["settings_schema"]["default_chat_mode"]["default"] == "reply" and len(m["description"]) <= 300
    assert not {"login_start", "logout", "send_message"} & {t["name"] for t in m["tools"]}      # агенту эти действия недоступны


async def test_panel_without_session_offers_qr_login_and_no_tiles(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        rig.mod.user._unauthorized = True
        blocks = await blocks_of(rig)
    assert find(blocks, "text", title="Аккаунт")[0]["text"] == "Не вошла в аккаунт."
    assert find(blocks, "buttons")[0]["items"][0]["action"] == "login_start"
    assert not find(blocks, "stats") and not [b for b in blocks if b.get("title") == "Последние диалоги"]
    assert find(blocks, "text", title="Канал и бот-витрина")[0]["collapsed"] is True


async def test_panel_login_waiting_password_and_error(tmp_path, monkeypatch):
    from tg_qr_login import QrLogin
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        user._unauthorized = True
        login = user._login = QrLogin(str(rig.mem / "tg_user"), "1", "h", user.qr_dir, client_factory=lambda: None)
        login.state, login.qr_file, login.expires = "waiting", "qr-ab12.png", login._clock() + 20
        blocks = rig.mod.panel.account_blocks()
        assert find(blocks, "image")[0]["file"] == "qr-ab12.png" and find(blocks, "progress")[0]["value"] <= 1
        assert find(blocks, "buttons")[0]["items"][0]["action"] == "login_cancel"
        login.state, login.error = "password", ""
        field = find(rig.mod.panel.account_blocks(), "input")[0]
        assert field["secret"] is True and field["action"] == "login_password" and "value" not in field
        login.state, login.error = "error", "нет сети: не удалось связаться с Telegram"
        assert "нет сети" in find(rig.mod.panel.account_blocks(), "text")[0]["text"]


async def test_password_action_goes_straight_to_the_login_and_is_not_echoed(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        user = rig.mod.user
        got = []
        user.submit_login_password = lambda value: got.append(value) or True
        out = await act(rig, "login_password", {"value": "СЕКРЕТ-123"})
        assert out["ok"] is True and "СЕКРЕТ" not in json.dumps(out, ensure_ascii=False) and got == ["СЕКРЕТ-123"]
        user.submit_login_password = lambda value: False
        out = await act(rig, "login_password", {"value": "СЕКРЕТ-123"})
        assert out["ok"] is False and "СЕКРЕТ" not in json.dumps(out, ensure_ascii=False)
        assert "СЕКРЕТ" not in json.dumps(await blocks_of(rig), ensure_ascii=False)


async def test_dialog_list_is_cards_with_mode_select_and_two_columns(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER,
                                                                         "default_chat_mode": "read"}) as rig:
        install(rig, [Dlg(5, "Маша", "user", 2, "привет"), Dlg(OWNER, "Аня", "user"), Dlg(-1, "Соседи", "group", 0, "ау", out=True),
                      Dlg(-1002, "Клуб", "super"), Dlg(-1003, "Новости", "channel", 9)])
        rig.mod.user.modes.set(-1, "ignore")
        blocks = await blocks_of(rig)
    split = [b for b in blocks if b["type"] == "split"][0]
    assert split["left"] and split["right"][0]["type"] == "text"                       # чат не открыт: подсказка справа
    by = cards(blocks)
    assert set(by) == {"Маша", "Аня", "Соседи", "Клуб", "Новости"}
    assert by["Маша"]["badge"] == "2" and by["Маша"]["subtitle"] == "привет" and by["Маша"]["action"] == "open_chat"
    assert by["Маша"]["args"] == {"chat": 5} and by["Маша"]["select"]["value"] == "read"       # по умолчанию «чтение»
    assert [o["value"] for o in by["Маша"]["select"]["options"]] == ["ignore", "read", "reply"]
    assert by["Маша"]["select"]["action"] == "set_mode" and by["Маша"]["select"]["arg"] == "mode"
    assert by["Соседи"]["subtitle"] == "Вы: ау" and by["Соседи"]["select"]["value"] == "ignore" and by["Соседи"]["muted"] is True
    assert "select" not in by["Аня"] and by["Аня"]["tag"] == "владелец"                         # режим владельца не переключается
    assert by["Новости"]["tag"] == "канал" and "children" not in by["Клуб"]


async def test_set_mode_persists_and_rejects_owner_and_bad_mode(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True, module_settings={"owner_tg_id": OWNER}) as rig:
        install(rig, [Dlg(-1, "Соседи", "group")])
        assert (await act(rig, "set_mode", {"chat": -1, "mode": "read"}))["ok"] is True
        assert rig.mod.user.modes.get(-1) == "read"
        assert (await act(rig, "set_mode", {"chat": -1, "mode": "write"}))["ok"] is False
        assert (await act(rig, "set_mode", {"chat": OWNER, "mode": "ignore"}))["ok"] is False
        assert (await act(rig, "set_mode", {"chat": "x", "mode": "read"}))["ok"] is False
        assert rig.mod.user.modes.explicit(OWNER) is None


async def test_open_chat_history_reply_send_delete_mark_read(tmp_path, monkeypatch):
    msgs = [Msg(8, "привет"), Msg(9, "мой ответ", out=True), Msg(11, "а вот ещё", reply=9)]
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(5, "Маша", "user")], msgs)
        assert (await act(rig, "open_chat", {"chat": 5}))["ok"] is True
        blocks = await blocks_of(rig)
        table = find(blocks, "table")[0]
        assert [r["cells"][0] for r in table["rows"]] == [8, 9, 11]                      # по порядку, старые первыми
        assert table["rows"][1]["cells"][2] == "→ она" and table["rows"][1]["cells"][4] == "✓✓"      # прочитано: id 9 <= 10
        assert table["rows"][2]["cells"][3].startswith("↩#9 ") and table["rows"][2]["cells"][4] == ""
        assert [a["id"] for a in table["rows"][0]["actions"]] == ["reply_to"]
        assert [a["id"] for a in table["rows"][1]["actions"]] == ["reply_to", "delete_message"]   # удалять только своё
        assert find(blocks, "input")[-1]["label"] == "Написать от лица агента"
        head = find(blocks, "cards")[-1]["items"][0]                                       # шапка чата: название и режим
        assert head["title"] == "Маша" and head["select"]["value"] == "reply" and head["select"]["args"] == {"chat": 5}
        assert (await act(rig, "reply_to", {"chat": 5, "id": 8}))["ok"] is True
        assert "ответом на #8" in find(await blocks_of(rig), "input")[-1]["label"]
        assert (await act(rig, "send_message", {"chat": 5, "value": "  привет!  "}))["ok"] is True
        assert rig.tl.sent[-1] == (("chat", 5), "привет!") and rig.mod.panel.reply_to is None
        assert rig.mod.user.dialogs.activity(5)["hour"] == 1                             # попало в её активность как её сообщение
        assert (await act(rig, "send_message", {"chat": 5, "value": "   "}))["ok"] is False
        assert (await act(rig, "delete_message", {"chat": 5, "id": 9}))["ok"] is True and rig.tl.deleted == [[9]]
        assert (await act(rig, "delete_message", {"chat": 5, "id": 8}))["ok"] is False and rig.tl.deleted == [[9]]
        assert (await act(rig, "mark_read", {"chat": 5}))["ok"] is True and rig.tl.reads == [("chat", 5)]
        assert (await act(rig, "close_chat"))["ok"] is True
        assert find(await blocks_of(rig), "table") == [] and find(await blocks_of(rig), "cards")[0]["items"][0]["title"] == "Маша"


async def test_owner_sends_from_panel_in_read_and_ignore_but_label_says_so(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(-1, "Соседи", "group")], [Msg(1, "x")])
        rig.mod.user.modes.set(-1, "read")
        await act(rig, "open_chat", {"chat": -1})
        assert "от имени владельца (режим без ответов)" in find(await blocks_of(rig), "input")[-1]["label"]
        assert (await act(rig, "send_message", {"chat": -1, "value": "от владельца"}))["ok"] is True
        rig.mod.user.modes.set(-1, "ignore")
        assert (await act(rig, "send_message", {"chat": -1, "value": "и в игноре"}))["ok"] is True
        assert [t for _, t in rig.tl.sent] == ["от владельца", "и в игноре"]


async def test_paging_search_history_more_and_logout_double_press(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(i, f"Чат {i}", "user") for i in range(1, 121)], [Msg(i, "m") for i in range(1, 60)])
        blocks = await blocks_of(rig)
        assert len(find(blocks, "cards")[0]["items"]) == 50
        assert any(b["items"][0]["action"] == "more_chats" for b in find(blocks, "buttons"))
        await act(rig, "more_chats")
        assert len(find(await blocks_of(rig), "cards")[0]["items"]) == 50                # блок cards держит не больше 50
        await act(rig, "filter_chats", {"value": "Чат 11"})
        assert set(cards(await blocks_of(rig))) >= {"Чат 11", "Чат 110"}
        await act(rig, "filter_chats", {"clear": True})
        await act(rig, "open_chat", {"chat": 1})
        assert find(await blocks_of(rig), "buttons")[-1]["items"][0]["action"] == "history_more"
        await act(rig, "history_more", {"chat": 1})
        assert rig.mod.panel.history_limit == 100
        await act(rig, "close_chat")
        assert find(await blocks_of(rig), "buttons")[0]["items"][0]["label"] == "Выйти из аккаунта"
        out = await act(rig, "logout")
        assert out["ok"] and "ещё раз" in out["message"] and (rig.mem / "tg_user.session").exists()
        assert "Точно выйти" in find(await blocks_of(rig), "buttons")[0]["items"][0]["label"]
        calls = []

        async def log_out():
            calls.append(1)

        rig.tl.log_out = log_out
        rig.tl.disconnect = lambda: asyncio.sleep(0)
        rig.mod.user._client = rig.tl
        out = await act(rig, "logout")
        assert out["ok"] and calls == [1] and not (rig.mem / "tg_user.session").exists()


async def test_panel_size_stays_under_the_limit_with_long_texts(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(1, "Чат", "user")], [Msg(i, "я" * 600) for i in range(1, 201)])
        await act(rig, "open_chat", {"chat": 1})
        for _ in range(3):
            await act(rig, "history_more", {"chat": 1})
        blocks = await blocks_of(rig)
        assert len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()) <= P.PANEL_SET_MAX_BYTES


async def test_dialogs_error_is_shown_not_raised(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [])

        async def broken(limit=200):
            raise ConnectionError("нет маршрута")
            yield

        rig.tl.iter_dialogs = broken
        blocks = await blocks_of(rig)
        assert "нет сети" in find(blocks, "text", title="Чаты")[0]["text"]


async def test_panel_reaches_core_through_run_state_and_actions_work_over_the_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "STATE_POLL_S", 0.05)
    async with Rig(tmp_path, monkeypatch, telethon=True) as rig:
        install(rig, [Dlg(5, "Маша", "user", 1, "привет")])
        task = asyncio.create_task(rig.mod.run_state())
        try:
            await rig.core.wait_for(lambda: rig.core.panels.get("telegram"))
            await rig.core.panel_action("open_chat", {"chat": 5})
            await rig.core.wait_for(lambda: any(b.get("action") == "send_message" for b in flat(rig.core.panels["telegram"]["blocks"])))
        finally:
            task.cancel()
        assert rig.core.violations == []
        assert (await rig.core.panel_action("refresh"))["ok"] is True
        with pytest.raises(ToolError):
            await rig.core.panel_action("не_такое")
