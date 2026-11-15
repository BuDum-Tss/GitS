"""Панель Discord: статус, приглашение, карточки серверов и режимов, личка, открытый чат, аватарки с таймаутами, размер."""
import asyncio
import json
import time

import discord_bot
import discord_module as app_mod
import pytest
from dc_rig import OWNER_ID, Asset, Channel, Msg, Rig, User

from gits_sdk import ToolError
from gits_sdk import protocol as P


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


async def blocks_of(rig):
    """Панель целиком; тело проверено настоящим validate_panel_set (с действиями из манифеста) и размером."""
    rig.mod.panel.invalidate()
    blocks = await asyncio.wait_for(rig.mod.panel_blocks(), app_mod.PANEL_BUILD_TIMEOUT_S)
    actions = set(rig.client.manifest["panel"]["actions"])
    assert P.validate_panel_set({"blocks": blocks}, actions) == [], blocks
    assert len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode()) <= P.PANEL_SET_MAX_BYTES
    return blocks


async def act(rig, action, args=None):
    return await rig.core.panel_action(action, args or {})


def guild_cards(blocks):
    cards = find(blocks, "cards")[0]["items"]
    return {c["title"]: c for c in cards}


def world(rig, with_icons=False):
    g, (general, quiet) = rig.server(1, "Клуб", (("общий", 100), ("тихий", 200)))
    if with_icons:
        g.icon = Asset("abc123")
    thread = rig.dc.register(g.add(Channel(300, "ветка про игру", "public_thread", parent=general)))
    return g, general, quiet, thread


async def test_manifest_declares_all_panel_actions_and_serve_dirs():
    from manifest import build_manifest
    from discord_panel import ACTIONS
    m = build_manifest(["C:/x/discord_avatars"])
    assert m["panel"]["actions"] == list(ACTIONS) and m["panel"]["serve_dirs"] == ["C:/x/discord_avatars"]
    assert P.validate_manifest(m) == [] and len(m["description"]) <= 300
    assert not {"send_message", "set_mode", "delete_message"} & {t["name"] for t in m["tools"]}        # агенту эти действия недоступны


async def test_panel_without_token_says_not_configured_and_has_no_lists(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None, connected=False) as rig:
        blocks = await blocks_of(rig)
    text = find(blocks, "text", title="Бот")[0]["text"]
    assert text.startswith("Не настроено") and "DISCORD_BOT_TOKEN" in text and "Message Content Intent" in text
    assert not find(blocks, "cards") and not find(blocks, "split") and not find(blocks, "link")


async def test_panel_explains_which_intent_to_enable_and_offers_retry(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, connected=False) as rig:
        class PrivilegedIntentsRequired(Exception):
            pass
        rig.bot.classify(PrivilegedIntentsRequired("4014"))
        assert rig.bot.state_word() == "нет intent"
        blocks = await blocks_of(rig)
        text = find(blocks, "text", title="Бот")[0]["text"]
        assert "Message Content Intent" in text and "Privileged Gateway Intents" in text
        assert find(blocks, "buttons")[0]["items"][0]["action"] == "reconnect"
        assert (await act(rig, "reconnect"))["ok"] is True and rig.bot._wake.is_set()


async def test_status_and_invite_link_use_the_logged_in_bots_application_id(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        world(rig)
        blocks = await blocks_of(rig)
        assert find(blocks, "text", title="Бот")[0]["text"] == "Вошла как ГЛаДОС. Серверов: 1."
        link = find(blocks, "link")[0]
    perms = (1 << 10) | (1 << 11) | (1 << 38) | (1 << 16) | (1 << 14) | (1 << 15) | (1 << 6)
    assert link["url"] == f"https://discord.com/oauth2/authorize?client_id=42&scope=bot&permissions={perms}"
    assert perms == 274878024768 and "Пригласить" in link["label"]


async def test_guild_cards_with_mode_select_expand_to_channels_and_threads(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"default_chat_mode": "read"}) as rig:
        world(rig)
        rig.bot.modes.set("channel", 200, "ignore")
        blocks = await blocks_of(rig)
        club = guild_cards(blocks)["Клуб"]
        assert club["subtitle"] == "каналов: 2, веток и постов: 1" and club["action"] == "toggle_guild" and club["args"] == {"id": "1"}
        assert club["select"]["value"] == "read" and [o["value"] for o in club["select"]["options"]] == ["ignore", "read", "reply"]
        assert club["select"]["args"] == {"scope": "guild", "id": "1"} and club["select"]["arg"] == "mode" and "children" not in club
        assert (await act(rig, "toggle_guild", {"id": "1"}))["ok"] is True
        club = guild_cards(await blocks_of(rig))["Клуб"]
        titles = [c["title"] for c in club["children"]]
        assert club["expanded"] is True and titles == ["#общий", "↳ ветка про игру", "#тихий"]
        general, thread, quiet = club["children"]
        assert general["select"]["value"] == "inherit" and general["select"]["options"][0] == {"value": "inherit", "label": "Как в сервере (чтение)"}
        assert general["action"] == "open_chat" and general["args"] == {"target": "channel:100"}
        assert thread["tag"] == "ветка" and thread["args"] == {"target": "channel:300"} and thread["select"]["options"][0]["label"] == "Как в канале (чтение)"
        assert thread["select"]["args"] == {"scope": "thread", "id": "300"}
        assert quiet["select"]["value"] == "ignore" and quiet["muted"] is True
        await act(rig, "toggle_guild", {"id": "1"})
        assert "children" not in guild_cards(await blocks_of(rig))["Клуб"]


async def test_forum_posts_are_listed_under_the_forum_and_forum_is_not_openable(tmp_path, monkeypatch):
    from types import SimpleNamespace
    async with Rig(tmp_path, monkeypatch) as rig:
        g, (forum,) = rig.server(1, "Клуб", (("идеи", 200),))
        forum.type = SimpleNamespace(name="forum")
        g.add(Channel(400, "пост", "public_thread", parent=forum))
        await act(rig, "toggle_guild", {"id": "1"})
        kids = guild_cards(await blocks_of(rig))["Клуб"]["children"]
    assert kids[0]["title"] == "идеи" and kids[0]["tag"] == "форум" and "action" not in kids[0]
    assert kids[1]["tag"] == "пост" and kids[1]["args"] == {"target": "channel:400"} and kids[1]["select"]["options"][0]["label"].startswith("Как в канале")


async def test_dm_group_lists_interlocutors_with_modes_and_owner_without_select(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        world(rig)
        masha, owner = User(5, "masha", "Маша"), User(OWNER_ID, "anya", "Аня")
        rig.dc.private_channels += [Channel(9001, "dm", "dm", None, recipient=masha), Channel(9002, "dm", "dm", None, recipient=owner),
                                    Channel(9003, "dm", "dm", None, recipient=User(8, "bot", bot=True))]
        rig.bot.modes.set("dm", 5, "ignore")
        blocks = await blocks_of(rig)
        group = find(blocks, "cards")[0]["items"][0]
        assert group["title"] == "Личные сообщения" and group["tag"] == "2" and "children" not in group
        await act(rig, "toggle_guild", {"id": "dm"})
        kids = {c["title"]: c for c in find(await blocks_of(rig), "cards")[0]["items"][0]["children"]}
    assert set(kids) == {"Маша", "Аня"} and kids["Маша"]["select"]["value"] == "ignore" and kids["Маша"]["muted"] is True
    assert kids["Маша"]["args"] == {"target": "dm:5"} and "select" not in kids["Аня"] and kids["Аня"]["tag"] == "владелец"


async def test_set_mode_persists_inherits_and_rejects_bad_input_and_owner_dm(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"owner": str(OWNER_ID)}) as rig:
        world(rig)
        assert (await act(rig, "set_mode", {"scope": "guild", "id": "1", "mode": "read"}))["ok"] is True
        assert rig.bot.modes.explicit("guild", 1) == "read"
        assert (await act(rig, "set_mode", {"scope": "channel", "id": "100", "mode": "ignore"}))["ok"] is True
        assert (await act(rig, "set_mode", {"scope": "thread", "id": "300", "mode": "reply"}))["ok"] is True
        assert (await act(rig, "set_mode", {"scope": "channel", "id": "100", "mode": "inherit"}))["ok"] is True
        assert rig.bot.modes.explicit("channel", 100) is None and rig.bot.modes.explicit("thread", 300) == "reply"
        for args in ({"scope": "guild", "id": "1", "mode": "inherit"}, {"scope": "guild", "id": "1", "mode": "write"},
                     {"scope": "nope", "id": "1", "mode": "read"}, {"scope": "guild", "id": "x", "mode": "read"},
                     {"scope": "dm", "id": str(OWNER_ID), "mode": "ignore"}):
            assert (await act(rig, "set_mode", args))["ok"] is False, args
        assert rig.bot.modes.explicit("dm", OWNER_ID) is None and rig.bot.modes.explicit("guild", 1) == "read"


async def test_open_chat_history_reply_send_delete_and_more(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, thread = world(rig)
        masha = User(5, "masha", "Маша")
        mine = Msg(9, "мой ответ", rig.me, general, minutes=2)
        general.messages = [Msg(8, "привет", masha, general, minutes=1), mine, Msg(11, "а вот ещё", masha, general, minutes=3,
                                                                                 reference=type("R", (), {"message_id": 9})())]
        assert (await act(rig, "open_chat", {"target": "channel:100"}))["ok"] is True
        blocks = await blocks_of(rig)
        table = find(blocks, "table")[0]
        assert [r["cells"][0] for r in table["rows"]] == ["8", "9", "11"] and table["rows"][1]["cells"][2] == "→ она"
        assert table["rows"][2]["cells"][3].startswith("↩#9 ")
        assert [a["id"] for a in table["rows"][0]["actions"]] == ["reply_to"]
        assert [a["id"] for a in table["rows"][1]["actions"]] == ["reply_to", "delete_message"]        # удалять только своё
        head = find(blocks, "cards")[-1]["items"][0]
        assert head["title"] == "Клуб › #общий" and "Режим: «ответы»" in head["subtitle"]
        assert find(blocks, "input")[-1]["label"] == "Написать от лица агента"
        assert (await act(rig, "reply_to", {"target": "channel:100", "id": "8"}))["ok"] is True
        assert "ответом на #8" in find(await blocks_of(rig), "input")[-1]["label"]
        assert (await act(rig, "send_message", {"target": "channel:100", "value": "  привет!  "}))["ok"] is True
        assert general.sent[-1]["content"] == "привет!" and general.sent[-1]["reference"] == ("partial", 8) and rig.mod.panel.reply_to is None
        assert rig.bot.dialogs.activity("channel:100")["hour"] == 1
        assert (await act(rig, "send_message", {"target": "channel:100", "value": "   "}))["ok"] is False
        assert (await act(rig, "delete_message", {"target": "channel:100", "id": "9"}))["ok"] is True and mine.deleted is True
        assert (await act(rig, "delete_message", {"target": "channel:100", "id": "8"}))["ok"] is False
        assert (await act(rig, "close_chat"))["ok"] is True and not find(await blocks_of(rig), "table")
        assert (await act(rig, "open_chat", {"target": "tg:5"}))["ok"] is False


async def test_history_paging_and_panel_size_stays_under_the_limit(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, quiet, thread = world(rig)
        masha = User(5, "masha", "Маша")
        general.messages = [Msg(i, "я" * 900, masha, general) for i in range(1, 201)]
        await act(rig, "open_chat", {"target": "channel:100"})
        assert find(await blocks_of(rig), "buttons")[-1]["items"][0]["action"] == "history_more"
        for _ in range(3):
            await act(rig, "history_more", {"target": "channel:100"})
        assert rig.mod.panel.history_limit == 200
        blocks = await blocks_of(rig)                                    # blocks_of проверяет и размер
        assert 0 < len(find(blocks, "table")[0]["rows"]) < 200           # старые строки урезаны, чтобы влезть


async def test_search_filters_guilds_and_clears(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        world(rig)
        rig.server(2, "Школа", (("уроки", 500),))
        assert set(guild_cards(await blocks_of(rig))) == {"Клуб", "Школа"}
        await act(rig, "filter_guilds", {"value": "шко"})
        assert set(guild_cards(await blocks_of(rig))) == {"Школа"}
        await act(rig, "filter_guilds", {"clear": True})
        assert len(guild_cards(await blocks_of(rig))) == 2


async def test_owner_sends_from_panel_in_read_mode_and_label_says_so(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = world(rig)
        rig.bot.modes.set("channel", 100, "read")
        await act(rig, "open_chat", {"target": "channel:100"})
        assert "от имени владельца (режим без ответов)" in find(await blocks_of(rig), "input")[-1]["label"]
        assert (await act(rig, "send_message", {"target": "channel:100", "value": "от владельца"}))["ok"] is True


# ── аватарки: CDN может быть недоступен ───────────────────────────────────────────────────────────

async def test_avatar_downloads_for_visible_cards_only_and_not_more_than_20(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        for i in range(1, 31):
            rig.dc.guild(i, f"Сервер {i:02d}", Asset(f"key{i}", f"https://cdn.discordapp.com/icons/{i}/k.png"))
        asked = []

        async def download(url):
            asked.append(url)
            return b"\x89PNG-data"
        rig.bot._download = download
        blocks = await blocks_of(rig)
        cards = find(blocks, "cards")[0]["items"]
        assert len(asked) == discord_bot.AVATAR_NEW_MAX == 20 and sum(1 for c in cards if "avatar" in c) == 20
        assert (rig.bot.avatar_dir / "dc_g1_key1.png").read_bytes() == b"\x89PNG-data"
        assert cards[0]["avatar"] == "dc_g1_key1.png"
        await blocks_of(rig)                                           # следующее обновление: ещё 10, готовые из файлов
        assert len(asked) == 30


async def test_avatar_not_downloaded_from_foreign_hosts(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.dc.guild(1, "Клуб", Asset("k", "https://evil.example.com/a.png"))
        rig.dc.guild(2, "Клуб2", Asset("k2", "http://cdn.discordapp.com/a.png"))
        asked = []

        async def download(url):
            asked.append(url)
            return b"x"
        rig.bot._download = download
        blocks = await blocks_of(rig)
    assert asked == [] and not any("avatar" in c for c in find(blocks, "cards")[0]["items"])


async def test_hanging_avatar_download_does_not_freeze_the_panel_and_three_timeouts_disable_avatars(tmp_path, monkeypatch):
    monkeypatch.setattr(discord_bot, "AVATAR_TIMEOUT_S", 0.05)
    async with Rig(tmp_path, monkeypatch) as rig:
        for i in range(1, 8):
            rig.dc.guild(i, f"С{i}", Asset(f"k{i}", f"https://cdn.discordapp.com/icons/{i}/k.png"))
        calls = []

        async def hang(url):
            calls.append(url)
            await asyncio.sleep(3600)
        rig.bot._download = hang
        started = time.monotonic()
        for _ in range(3):                                              # зависание: одно таймаут-скачивание за обновление
            blocks = await blocks_of(rig)
            assert not any("avatar" in c for c in find(blocks, "cards")[0]["items"]) and len(find(blocks, "cards")[0]["items"]) == 7
        assert time.monotonic() - started < 3 and len(calls) == 3
        assert rig.bot._avatar_off_until > time.monotonic()            # три таймаута подряд: аватарки выключены на 10 минут
        await blocks_of(rig)
        assert len(calls) == 3                                          # больше не пробуем
        rig.bot._avatar_off_until = 0.0
        rig.bot._avatar_fail.clear()
        rig.bot._download = lambda url: asyncio.sleep(0, result=b"png")  # CDN ожил
        assert any("avatar" in c for c in find(await blocks_of(rig), "cards")[0]["items"])


async def test_failed_avatar_is_retried_not_every_refresh(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.dc.guild(1, "Клуб", Asset("k", "https://cdn.discordapp.com/icons/1/k.png"))
        calls = []

        async def broken(url):
            calls.append(url)
            raise OSError("404")
        rig.bot._download = broken
        for _ in range(3):
            await blocks_of(rig)
        assert len(calls) == 1 and not list(rig.bot.avatar_dir.glob("*.tmp"))


async def test_hanging_history_gives_an_error_text_within_the_timeout(tmp_path, monkeypatch):
    import discord_panel
    monkeypatch.setattr(discord_panel, "HISTORY_TIMEOUT_S", 0.2)
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = world(rig)
        general.hang = True
        await act(rig, "open_chat", {"target": "channel:100"})
        started = time.monotonic()
        blocks = await blocks_of(rig)
        assert time.monotonic() - started < 3
        assert any("не ответил" in b.get("text", "") for b in flat(blocks))


async def test_history_forbidden_is_shown_as_text(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, general, *_ = world(rig)

        async def deny(limit=50):
            from dc_rig import Forbidden
            raise Forbidden("x")
            yield
        general.history = deny
        await act(rig, "open_chat", {"target": "channel:100"})
        blocks = await blocks_of(rig)
        assert any("нет прав" in b.get("text", "") and "История не получена" in b.get("text", "") for b in flat(blocks))


async def test_panel_reaches_core_through_run_state_and_actions_work_over_the_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "STATE_POLL_S", 0.05)
    async with Rig(tmp_path, monkeypatch) as rig:
        world(rig)
        task = asyncio.create_task(rig.mod.run_state())
        try:
            await rig.core.wait_for(lambda: rig.core.panels.get("discord"))
            assert rig.core.states["discord"]["summary"] == "бот: подключён, серверов: 1"
            await rig.core.panel_action("open_chat", {"target": "channel:100"})
            await rig.core.wait_for(lambda: any(b.get("action") == "send_message" for b in flat(rig.core.panels["discord"]["blocks"])))
        finally:
            task.cancel()
        assert rig.core.violations == []
        assert (await rig.core.panel_action("refresh"))["ok"] is True
        with pytest.raises(ToolError):
            await rig.core.panel_action("не_такое")


async def test_a_stuck_panel_build_is_cut_off_and_the_loop_keeps_running(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "STATE_POLL_S", 0.05)
    monkeypatch.setattr(app_mod, "PANEL_BUILD_TIMEOUT_S", 0.2)
    async with Rig(tmp_path, monkeypatch) as rig:
        world(rig)
        hang = {"on": True}
        real = rig.mod.panel_blocks

        async def blocks():
            if hang["on"]:
                await asyncio.sleep(3600)
            return await real()
        rig.mod.panel_blocks = blocks
        task = asyncio.create_task(rig.mod.run_state())
        try:
            await rig.core.wait_for(lambda: rig.core.states.get("discord"))        # состояние уходит, хотя панель висит
            hang["on"] = False
            await rig.core.wait_for(lambda: rig.core.panels.get("discord"), timeout=3)   # и панель оживает без перезапуска
        finally:
            task.cancel()
