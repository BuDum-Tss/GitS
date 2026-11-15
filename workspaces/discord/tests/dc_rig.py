"""Стенд тестов модуля Discord: настоящий WorkspaceClient против FakeCore, discord.py и сеть Discord подменены."""
import asyncio
import contextlib
import datetime
from pathlib import Path
from types import SimpleNamespace

from gits_sdk import WorkspaceClient
from gits_sdk.testing import FakeCore

from discord_module import DiscordModule
from manifest import build_manifest

SHARED = {"quiet_until": 0.0, "dnd": False}
TOKEN = ".".join(("MTIzNDU2Nzg5MDEyMzQ1Njc4", "GabcDE", "abcdefghijklmnopqrstuvwxyz0123456789"))       # только для тестов: настоящих токенов тут нет
BOT_ID = 42
OWNER_ID = 777


class Forbidden(Exception):
    """Подмена discord.Forbidden (по имени класса и status)."""
    status = 403


class NotFound(Exception):
    status = 404


class HTTPException(Exception):
    """Подмена discord.HTTPException: status и code ответа Discord (10014 Unknown Emoji, 30010 лимит реакций)."""

    def __init__(self, text, status=400, code=0):
        super().__init__(text)
        self.status, self.code = status, code


class User:
    def __init__(self, uid, name, display=None, bot=False, avatar=None):
        self.id, self.name, self.display_name, self.global_name, self.bot = uid, name, display or name, None, bot
        self.display_avatar, self.dm_channel = avatar, None
        self.created: list = []

    async def create_dm(self):
        self.created.append(1)
        if self.dm_channel is None:
            self.dm_channel = Channel(self.id + 5_000_000, "dm", "dm", None, recipient=self)
        return self.dm_channel


class _Typing:
    def __init__(self, log, channel):
        self.log, self.channel = log, channel

    async def __aenter__(self):
        self.log.append(self.channel.id)

    async def __aexit__(self, *exc):
        return False


class Channel:
    """Канал, ветка или личный канал: type.name text | forum | public_thread | dm; parent_id у веток."""

    def __init__(self, cid, name, kind="text", guild=None, parent=None, recipient=None, position=0):
        self.id, self.name, self.type, self.guild = cid, name, SimpleNamespace(name=kind), guild
        self.parent, self.parent_id, self.recipient, self.position = parent, parent.id if parent else None, recipient, position
        self.archived = self.locked = False
        self.sent: list[dict] = []
        self.typing_log: list = []
        self.messages: list = []
        self.deny = False
        self.hang = False

    def typing(self):
        return _Typing(self.typing_log, self)

    async def send(self, content, **kw):
        if self.hang:
            await asyncio.sleep(3600)
        if self.deny:
            raise Forbidden("Missing Permissions")
        self.sent.append({"content": content, **kw})

    def get_partial_message(self, mid):
        return ("partial", mid)

    async def history(self, limit=50):
        if self.hang:
            await asyncio.sleep(3600)
        for m in list(reversed(self.messages))[:limit]:
            yield m

    async def fetch_message(self, mid):
        for m in self.messages:
            if m.id == mid:
                return m
        raise NotFound("Unknown Message")


class Guild:
    def __init__(self, gid, name, icon=None):
        self.id, self.name, self.icon = gid, name, icon
        self.channels: list[Channel] = []
        self.threads: list[Channel] = []

    def add(self, channel):
        channel.guild = self
        (self.threads if channel.parent_id else self.channels).append(channel)
        return channel


class Asset:
    def __init__(self, key, url="https://cdn.discordapp.com/icons/1/x.png"):
        self.key, self.url = key, url


class FakeDiscord:
    """Клиент discord.py: кэш серверов, пользователей и каналов."""

    def __init__(self):
        self.guilds: list[Guild] = []
        self.users: list[User] = []
        self.private_channels: list[Channel] = []
        self.channels: dict[int, Channel] = {}
        self.application_id = BOT_ID
        self.fetched: list = []

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        self.fetched.append(cid)
        if cid in self.channels:
            return self.channels[cid]
        raise NotFound("Unknown Channel")

    def get_user(self, uid):
        return next((u for u in self.users if u.id == uid), None)

    async def fetch_user(self, uid):
        user = self.get_user(uid)
        if user is None:
            raise NotFound("Unknown User")
        return user

    async def close(self):
        return None

    # удобства стенда
    def guild(self, gid=1, name="Клуб", icon=None):
        g = Guild(gid, name, icon)
        self.guilds.append(g)
        return g

    def register(self, *channels):
        for c in channels:
            self.channels[c.id] = c
        return channels[0] if len(channels) == 1 else channels

    def user(self, uid, name, **kw):
        u = User(uid, name, **kw)
        self.users.append(u)
        return u


class Msg:
    """Сообщение discord.py (то, что приходит в on_message и в историю)."""

    def __init__(self, mid, content, author, channel, *, mentions=(), reference=None, attachments=(), stickers=(), embeds=(),
                 minutes=0, mtype="default", webhook=None):
        self.id, self.content, self.author, self.channel, self.guild = mid, content, author, channel, channel.guild
        self.mentions, self.reference, self.attachments, self.stickers, self.embeds = list(mentions), reference, attachments, stickers, embeds
        self.created_at, self.edited_at = datetime.datetime(2026, 10, 10, 12, minutes), None
        self.type, self.webhook_id, self.flags = SimpleNamespace(name=mtype), webhook, SimpleNamespace(voice=False)
        self.deleted = False
        self.reactions: list[str] = []
        self.react_error: Exception | None = None            # что бросит add_reaction / remove_reaction
        self.removed_by = None

    async def delete(self):
        self.deleted = True

    async def add_reaction(self, emoji):
        if self.react_error is not None:
            raise self.react_error
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, member):
        if self.react_error is not None:
            raise self.react_error
        self.reactions.remove(emoji)
        self.removed_by = member


def reply_ref(msg):
    return SimpleNamespace(message_id=msg.id, resolved=msg, cached_message=None)


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, settings=None, module_settings=None, terms=("Анна",), token=TOKEN, connected=True,
                 privacy_ready=True):
        self.tmp = Path(tmp_path)
        self.mem = self.tmp / "memory"
        self.mem.mkdir(parents=True, exist_ok=True)
        self.terms = list(terms)
        self.connected = connected
        self.asks: list[dict] = []
        if token:
            monkeypatch.setenv("DISCORD_BOT_TOKEN", token)
        self.core = FakeCore(settings={**SHARED, **(settings or {})}, module_settings=module_settings)
        self.dc = FakeDiscord()
        self._privacy_ready = privacy_ready
        self._task: asyncio.Task | None = None

    def _build(self):
        """Клиент и модуль создаются после старта FakeCore: адрес ядра известен только тогда."""
        self.client = WorkspaceClient(build_manifest(), url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.mod = DiscordModule(self.client, str(self.mem))
        self.bot = self.mod.bot
        self.bot.set_aliases(["ГЛаДОС", "glados"])
        self.bot.typing_scale = 0                                     # тесты не ждут «печатает»
        self.me = User(BOT_ID, "glados_bot", display="ГЛаДОС", bot=True)
        if self.connected:
            self.bot._client, self.bot._me = self.dc, self.me
        core, rig = self.core, self

        @core.on_request("core.privacy_terms")
        def privacy(body):
            return {"terms": rig.terms}

        @core.on_request("core.ask")
        def ask(body):
            rig.asks.append(body)
            return {"answer": True, "by": "user"}

    async def __aenter__(self):
        await self.core.start()
        self._build()
        self._task = asyncio.create_task(self.client.run())
        await self.core.wait_connected()
        await self.client.wait_connected(5)
        if self._privacy_ready:
            assert await self.mod.privacy.refresh()
        return self

    async def __aexit__(self, *exc):
        await self.mod.stop()
        await self.client.stop()
        if self._task is not None and not self._task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
        await self.core.stop()

    def events(self, kind=None):
        return [e for e in self.core.events if kind is None or e["kind"] == kind]

    async def say(self, message):
        """Сообщение приходит боту (on_message) и доходит до ядра."""
        await self.bot._on_incoming(message)
        await asyncio.sleep(0.05)

    # серверы и каналы для тестов
    def server(self, gid=1, name="Клуб", channels=(("общий", 100),)):
        g = self.dc.guild(gid, name)
        made = [self.dc.register(g.add(Channel(cid, cname))) for cname, cid in channels]
        return g, made
