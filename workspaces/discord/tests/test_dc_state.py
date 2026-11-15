"""Состояние модуля: нет токена, сбои входа, токен не попадает в журнал, манифест."""
import asyncio
import io
import logging

import pytest
from dc_rig import TOKEN, Rig

from common import TokenScrubFilter, configure_logging, scrub
from gits_sdk import ToolError
from gits_sdk import protocol as P
from manifest import MANIFEST, build_manifest


def test_manifest_is_valid_and_matches_the_task():
    assert P.validate_manifest(build_manifest()) == []
    m = MANIFEST
    assert m["id"] == "discord" and m["ownership"] == "owned" and m["role"] == "workspace" and len(m["description"]) <= 300
    assert {e["kind"]: e["default_urgency"] for e in m["emits"]} == {"discord.dm": 3, "discord.message": 1}
    schema = m["settings_schema"]
    assert set(schema) == {"owner", "names", "default_chat_mode", "dm_flood_per_10min", "dialog_window_mins",
                      "voice_announce", "voice_announce_text", "voice_alone_timeout_s", "voice_autojoin"}
    assert (schema["dm_flood_per_10min"]["default"], schema["dialog_window_mins"]["default"], schema["default_chat_mode"]["default"]) == (20, 15, "reply")
    assert schema["default_chat_mode"]["enum"] == ["ignore", "read", "reply"]
    assert not any(s.get("secret") for s in schema.values()) and "token" not in str(schema).lower()      # токен только в .env


async def test_without_token_module_works_and_says_not_configured(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, token=None, connected=False) as rig:
        assert rig.bot.configured() is False and rig.bot.state_word() == "не настроен"
        ok, why = rig.bot.state()
        assert ok is False and "DISCORD_BOT_TOKEN" in why
        summary, data = rig.mod.snapshot()
        assert summary == "бот: не настроен" and data["bot_ok"] is False
        for tool, args in (("message", {"to": "#общий", "text": "т"}), ("inbox", {"channel": "#общий"})):
            with pytest.raises(ToolError) as e:
                await rig.core.call_tool(tool, args)
            assert e.value.code == "unavailable" and "не настроен" in str(e.value)
        assert (await rig.core.call_tool("inbox", {}))                                  # обзор без подключения не падает
        with pytest.raises(ToolError) as e:
            await rig.core.request("deliver", {"target": "channel:1", "text": "т"})
        assert e.value.code == "unavailable"
        task = asyncio.create_task(rig.bot.run())                                       # цикл подключения без токена просто ждёт
        await asyncio.sleep(0.1)
        assert not task.done()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_login_failure_and_missing_library_are_states_not_crashes(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, connected=False) as rig:
        class LoginFailure(Exception):
            pass
        started = []

        class Client:
            async def start(self, token):
                started.append(token)
                raise LoginFailure("Improper token has been passed.")

            async def close(self):
                return None
        rig.bot._make_client = Client
        task = asyncio.create_task(rig.bot.run())
        await rig.core.wait_for(lambda: rig.bot._problem_kind == "token")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert started == [TOKEN] and rig.bot.state_word() == "неверный токен" and TOKEN not in rig.bot.state()[1]
        rig.bot._problem, rig.bot._problem_kind = "", ""
        monkeypatch.setattr(type(rig.bot), "lib_ok", staticmethod(lambda: False))
        assert rig.bot.state_word() == "нет discord.py" and "discord.py" in rig.bot.state()[1]


async def test_connection_error_is_classified_without_secrets(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, connected=False) as rig:
        rig.bot.classify(ConnectionError("нет сети"))
        assert rig.bot.state_word() == "нет сети"
        rig.bot.classify(RuntimeError(f"сбой с {TOKEN}"))
        assert rig.bot.state_word() == "ошибка связи" and TOKEN not in rig.bot.state()[1] and "***" in rig.bot.state()[1]


def test_scrub_removes_token_in_any_form():
    assert TOKEN not in scrub(f"Authorization: Bot {TOKEN}", TOKEN) and TOKEN not in scrub(f"url?t={TOKEN}")
    assert scrub("обычный текст без секретов") == "обычный текст без секретов"


def test_token_never_reaches_the_log_even_from_discord_and_aiohttp_loggers(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        configure_logging()
        configure_logging()                                                              # повторный вызов фильтр не дублирует
        assert sum(isinstance(f, TokenScrubFilter) for f in handler.filters) == 1
        assert logging.getLogger("discord").level == logging.WARNING and logging.getLogger("aiohttp").level == logging.WARNING
        logging.getLogger("discord.client").warning("Логин с токеном %s", TOKEN)
        logging.getLogger("aiohttp.client").error("GET https://discord.com/api?token=%s", TOKEN)
        logging.getLogger("discord.http").info("не должно попасть: уровень INFO у discord отключён %s", TOKEN)
        try:
            raise RuntimeError(f"ошибка с {TOKEN}")
        except RuntimeError:
            logging.getLogger("discord.gateway").exception("упало")
        logging.getLogger("gits_discord.bot").warning("модуль: %s", TOKEN[:-3] + "xyz")        # похожее на токен тоже скрывается
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    out = stream.getvalue()
    assert "Логин с токеном" in out and "упало" in out and "Traceback" in out
    assert TOKEN not in out and "gGabcDE" not in out and "abcdefghijklmnopqrstuvwxyz0123456789" not in out and "***" in out


async def test_token_is_not_in_tool_errors_panel_or_state(tmp_path, monkeypatch, caplog):
    async with Rig(tmp_path, monkeypatch) as rig:
        g, (general, _) = rig.server(1, "Клуб", (("общий", 100), ("тихий", 200)))
        rig.bot.known.add("channel:100", "общий")

        async def boom(content, **kw):
            raise RuntimeError(f"сбой соединения, токен {TOKEN}")
        general.send = boom
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("message", {"to": "#общий", "text": "привет"})
        assert TOKEN not in str(e.value)
        panel = await rig.core.panel_action("send_message", {"target": "channel:100", "value": "привет"})
        assert panel["ok"] is False and TOKEN not in str(panel)
        blocks = await rig.mod.panel_blocks()
        summary, data = rig.mod.snapshot()
        import json
        assert TOKEN not in json.dumps(blocks, ensure_ascii=False) and TOKEN not in summary + json.dumps(data)
        assert TOKEN not in caplog.text and TOKEN not in "".join(p.read_text(errors="ignore") for p in rig.mem.glob("*.json"))


async def test_client_is_built_with_the_required_intents_only():
    """Intents по заданию: guilds, guild_messages, dm_messages, message_content; без members и presence."""
    import sys
    fake = sys.modules["discord"]
    if not hasattr(fake, "__file__"):                                                   # подмена из conftest: проверяем её записи
        made = {}

        class Intents:
            def __init__(self):
                self.__dict__.update(guilds=False, guild_messages=False, dm_messages=False, message_content=False,
                                     members=False, presences=False)

            @classmethod
            def none(cls):
                return cls()

        class Client:
            def __init__(self, **kw):
                made.update(kw)
                self.intents = kw["intents"]

            def event(self, fn):
                return fn
        old = fake.Intents, fake.Client
        fake.Intents, fake.Client = Intents, Client
        try:
            from discord_bot import DiscordBot

            class Stub:
                module_settings = {}
            DiscordBot("x", Stub(), None)._make_client()
        finally:
            fake.Intents, fake.Client = old
        i = made["intents"]
        assert (i.guilds, i.guild_messages, i.dm_messages, i.message_content) == (True, True, True, True)
        assert (i.members, i.presences) == (False, False) and made["chunk_guilds_at_startup"] is False
