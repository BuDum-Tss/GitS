"""Модуль Discord против настоящих шины, внимания, реестра и маршрутизатора ядра (без процессов и без сети Discord).

Проверяет стык целиком: манифест принят, инструменты в реестре планировщика, обращение в канале идёт по R6 разговором, режим
«чтение» R6 не включает, личка чужого берётся единым агентом через внимание, ответ ядра доходит до канала (deliver).
"""
import asyncio
import contextlib
import copy
from types import SimpleNamespace

from dc_rig import BOT_ID, OWNER_ID, Channel, FakeDiscord, Msg, User
from hub_helpers import Core

from discord_module import DiscordModule
from environments import builtin
from gits_sdk import WorkspaceClient
from host import settings
from manifest import build_manifest
from services.conversation.conversation_io import Channel as CoreChannel
from services.conversation.conversation_io import ConversationIO


class Rel:
    def __init__(self):
        self.people: dict[str, dict] = {}

    def get(self, key):
        return self.people.get(key)

    def ensure_person(self, key, name, source="", **extra):
        new = key not in self.people
        self.people.setdefault(key, {"name": name, "source": source, "affinity": 0.0, "notes": []})
        return new

    def note(self, key, text):
        self.people[key]["notes"].append(text)


@contextlib.asynccontextmanager
async def stack(tmp_path, monkeypatch, *, module_settings=None):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789")))
    st = copy.deepcopy(settings.DEFAULTS)
    st["peers"] = {"discord": {"owner": str(OWNER_ID), **(module_settings or {})}}
    core = Core(tmp_path, st)
    core.companion._rel = Rel()
    core.companion._mem = SimpleNamespace(facts=[], add_fact=lambda t, kind="user", importance=3, due_days=None:
                                          core.companion._mem.facts.append((t, kind)) or True)
    core.companion.graph = SimpleNamespace(items=[], enqueue=lambda s, t: core.companion.graph.items.append((s, t)))
    core.requests.privacy_terms = lambda: ["Анна"]
    builtin.register_core_tools(core.envs, core.modules, core.attention)
    core.attention.hub = core.hub
    deliver_io = ConversationIO(core.companion, core.hub)         # обратный путь ответа (у core.io заглушка для входа обращений)
    await core.start()
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    client = WorkspaceClient(build_manifest(), url=core.url, token=core.token)
    mod = DiscordModule(client, str(mem))
    mod.bot.typing_scale = 0
    dc = FakeDiscord()
    me = User(BOT_ID, "glados_bot", display="ГЛаДОС", bot=True)
    mod.bot._client, mod.bot._me = dc, me
    g = dc.guild(1, "Клуб")
    general = dc.register(g.add(Channel(100, "общий")))
    task = asyncio.create_task(client.run())
    try:
        await core.wait_connected("discord")
        await client.wait_connected(5)
        assert await mod.privacy.refresh()
        yield SimpleNamespace(core=core, client=client, mod=mod, dc=dc, me=me, general=general, guild=g, io=deliver_io)
    finally:
        await mod.stop()
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await core.stop()


async def deliver(s, msg):
    await s.mod.bot._on_incoming(msg)
    await asyncio.sleep(0.3)


STRANGER = User(9, "petya", "Петя")


async def test_manifest_is_accepted_and_tools_reach_the_planner(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        rec = s.core.modules.get("discord")
        assert rec.connected and rec.title == "Discord" and "chat.channel" in rec.manifest["capabilities"]
        env = s.core.envs.envs["discord"]
        assert env.module == "discord" and {t.name for t in env.tools} == {"message", "react", "inbox", "voice_join", "voice_leave"}
        s.core.envs.focus = lambda: set()
        assert {"discord.message", "discord.inbox"} <= {t.id for t in s.core.envs.active_tools()}
        msg = s.core.envs.get("discord.message")
        assert msg.danger_check and msg.risk == "auto" and not msg.self_confirming


async def test_mention_in_channel_goes_through_r6_to_conversation(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        await deliver(s, Msg(1, "ГЛаДОС, ты тут?", STRANGER, s.general, mentions=[s.me]))
        assert s.core.attention.last_decision["rule"] == "R6"
        [(text, channel, sender)] = s.core.io.inputs
        assert text.startswith("Сообщение от Петя в сервере «Клуб», канале «#общий»") and sender["id"] == "discord:9"
        assert channel == {"module": "discord", "target": "channel:100"} or getattr(channel, "target", None) == "channel:100"


async def test_chatter_and_read_mode_do_not_trigger_r6(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        await deliver(s, Msg(1, "просто болтаем", STRANGER, s.general))
        assert s.core.io.inputs == []
        s.mod.bot.modes.set("channel", 100, "read")
        await deliver(s, Msg(2, "ГЛаДОС, ответь", STRANGER, s.general, mentions=[s.me]))
        assert s.core.io.inputs == [] and s.core.attention.last_decision["rule"] != "R6"
        s.mod.bot.modes.set("channel", 100, "ignore")
        before = s.core.attention.last_decision
        await deliver(s, Msg(3, "ГЛаДОС", STRANGER, s.general, mentions=[s.me]))
        assert s.core.attention.last_decision is before                      # игнор: события нет вовсе


async def test_stranger_dm_reaches_the_agent_through_attention_like_telegram(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        s.core.attention.set_alert_handler(lambda item: True)
        await deliver(s, Msg(1, "Привет, это Петя", STRANGER, Channel(9001, "dm", "dm", None, recipient=STRANGER)))
        assert s.core.attention.last_decision["decision"] == "D3" and s.core.attention.last_decision["rule"] is None
        [(text, channel, sender)] = s.core.io.inputs
        assert "Привет, это Петя" in text and sender["name"] == "Петя"
        assert "discord:9" in s.core.companion._rel.people


async def test_owner_message_in_channel_is_r1_and_owner_dm_too(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        owner = User(OWNER_ID, "anya", "Аня")
        await deliver(s, Msg(1, "ГЛаДОС, привет", owner, s.general, mentions=[s.me]))
        assert s.core.attention.last_decision["rule"] == "R1"
        await deliver(s, Msg(2, "как дела?", owner, Channel(9002, "dm", "dm", None, recipient=owner)))
        assert s.core.attention.last_decision["rule"] == "R1" and len(s.core.io.inputs) == 2


async def test_reply_from_the_core_is_delivered_to_the_channel_and_owner_question_to_dm(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        owner = s.dc.user(OWNER_ID, "anya", display="Аня")
        assert await s.io.deliver(CoreChannel("module", "discord", "channel:100"), "Я здесь, Петя.")
        assert s.general.sent[-1]["content"] == "Я здесь, Петя."
        assert await s.io.deliver(CoreChannel("module", "discord", "owner"), "Удалить черновые рендеры? Ответь да или нет.")
        assert owner.dm_channel.sent[-1]["content"].startswith("Удалить черновые")
        s.mod.bot.modes.set("channel", 100, "read")
        assert not await s.io.deliver(CoreChannel("module", "discord", "channel:100"), "ещё одно")      # режим закрыт: ядро видит отказ
        assert len(s.general.sent) == 1


async def test_message_through_the_router_asks_only_for_unknown_channels(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        tool = s.core.envs.get("discord.message")
        assert await tool.check({"to": "#общий", "text": "привет"})                         # ей ещё не писали: причина, роутер спросит
        assert await tool.check({"to": "owner", "text": "привет"}) is None
        s.mod.bot.known.add("channel:100", "общий")
        assert await tool.check({"to": "#общий", "text": "привет"}) is None
        assert await tool.check({"to": "#общий", "text": "Анна просила привет"})            # личные слова: причина
        ok, text = await asyncio.wait_for(s.core.router.run_tool("discord.message", {"to": "#общий", "text": "привет"}), 10)
        assert ok and s.general.sent[-1]["content"] == "привет"


async def test_disabling_the_module_drops_its_events(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch) as s:
        s.core.settings_state["peers"]["discord"]["enabled"] = False
        await s.core.hub.refresh()
        await s.core.wait_for(lambda: not s.client.enabled)
        before = s.core.attention.last_decision
        await s.client.event("discord.dm", "Сообщение от Петя в её личке: «привет»", addressed_to_me=True)
        await asyncio.sleep(0.2)
        assert s.core.attention.last_decision is before
