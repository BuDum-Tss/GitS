"""Модуль Discord: сборка клиента, инструментов, доставки ответов (`deliver`) и состояния для дашборда (по образцу telegram_module.py).

`DiscordModule(client, memory_dir)` вешает обработчики на `WorkspaceClient` и создаёт фоновые задачи (`start()`): личные слова от
ядра (privacy), бот Discord (discord.py), состояние и панель раз в 5 секунд.
Инструменты: `react` (реакция-эмодзи на сообщение; режимы и разрешение как у отправки), `message` (владельцу в личку, в канал, ветку, пост форума или человеку, который уже писал боту; разрешение
спрашивается по адресату) и `inbox` (чтение: серверы, канал, недавнее). Панель в дашборде (`discord_panel.py`) и её действия
(смена режима, ручная отправка владельцем, удаление своего) это `panel_action`, а не инструменты: агенту они недоступны.
Тесты собирают модуль против `gits_sdk.testing.FakeCore`, а discord.py подменяют (клиент `bot._client`).
"""
import asyncio
import json
import logging
import time

from common import parse_target, scrub, split_place
from discord_bot import ChatModeError, DiscordBot, LeakError, SendDenied
from discord_panel import ACTIONS as PANEL_ACTIONS
from discord_panel import DiscordPanel
from discord_voice import DiscordVoiceAdapter, VoiceError
from gits_sdk import ToolError, WorkspaceClient
from privacy import Privacy

logger = logging.getLogger("gits_discord.app")

STATE_EVERY_S = 300.0            # состояние при изменении и раз в 5 минут
STATE_POLL_S = 5.0
PANEL_BUILD_TIMEOUT_S = 25.0     # сборка панели (история, аватарки) дольше этого прерывается, цикл идёт дальше


def _text_arg(args: dict, name: str, required: bool = True) -> str:
    value = args.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"параметр «{name}» должен быть непустой строкой", code="bad_request")
    return value


def _int_arg(args: dict, name: str, default: int) -> int:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"параметр «{name}» должен быть числом", code="bad_request")
    return int(value)


class DiscordModule:
    def __init__(self, client: WorkspaceClient, memory_dir: str):
        self.client = client
        self.memory_dir = memory_dir
        self.privacy = Privacy(client.privacy_terms)
        self.bot = DiscordBot(memory_dir, client, self.privacy)
        self.voice = DiscordVoiceAdapter(self.bot)
        self.bot.voice = self.voice
        self.panel = DiscordPanel(self)
        self.bot.on_change = self.panel.wake.set          # подключение, сбой и выход обновляют панель сразу
        self.tasks: list[asyncio.Task] = []
        self._panel_key: str | None = None                # последние блоки пространства, посланные ядру
        self._register()

    # ── фоновые задачи ───────────────────────────────────────────────────────

    async def _privacy_loop(self) -> None:
        await self.client.wait_connected()
        await self.privacy.run()

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        for coro in (self._privacy_loop(), self.bot.run(), self.run_state(), self._persona_loop()):
            self.tasks.append(loop.create_task(coro))
        return self.tasks

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.voice.close()
        await self.bot.close()

    # ── состояние для дашборда ───────────────────────────────────────────────

    def snapshot(self) -> tuple[str, dict]:
        """(summary, data): статус бота, серверы, непрочитанные. Читает файлы: звать через to_thread."""
        bot = self.bot
        word = bot.state_word() if self.client.enabled else "выключен"
        data = {"bot_ok": bot.state_word() == "подключён", "guilds": bot.guild_count(), "unread": bot.unread_count(),
                "voice_sessions": len(self.voice.sessions)}
        return f"бот: {word}" + (f", серверов: {data['guilds']}" if data["bot_ok"] else ""), data

    async def panel_blocks(self) -> list[dict]:
        return await self.panel.blocks()

    async def run_state(self) -> None:
        last_key, sent_at = None, 0.0
        while True:
            try:
                summary, data = await asyncio.to_thread(self.snapshot)
                key = (summary, json.dumps(data, sort_keys=True))
                if key != last_key or time.monotonic() - sent_at >= STATE_EVERY_S:
                    await self.client.state(summary, data)
                    last_key, sent_at = key, time.monotonic()
                blocks = await asyncio.wait_for(self.panel_blocks(), PANEL_BUILD_TIMEOUT_S)    # зависание не замораживает панель
                pkey = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                if pkey != self._panel_key and await self.client.panel.set(blocks):     # частоту держит PanelClient.set
                    self._panel_key = pkey
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("состояние не отправлено: %s", type(e).__name__)
            try:
                await asyncio.wait_for(self.panel.wake.wait(), STATE_POLL_S)
            except asyncio.TimeoutError:
                pass
            self.panel.wake.clear()

    # ── обработчики ──────────────────────────────────────────────────────────

    async def load_persona(self) -> None:
        """Персонаж от ядра (core.persona): алиасы, по которым к ней обращаются в канале. Повторно при теме character."""
        try:
            card = (await self.client.persona()).raw
        except Exception as e:
            logger.warning("персона от ядра не получена: %s", scrub(e))
            return
        self.bot.set_aliases(card.get("aliases") or [])

    async def _persona_loop(self) -> None:
        await self.client.wait_connected()
        await self.load_persona()

    def _register(self) -> None:
        c = self.client

        @c.on_topic("character")
        async def _on_character(data):
            await self.load_persona()

        @c.tool("text")                                     # явная дорожка текстового ответа; message его прежнее имя (алиас)
        async def text(args):
            return await self.message(args)

        @c.check("text")
        async def text_check(args):
            return await self.message_check(args)

        @c.tool("message")
        async def message(args):
            return await self.message(args)

        @c.check("message")
        async def message_check(args):
            return await self.message_check(args)

        @c.tool("react")
        async def react(args):
            return await self.react(args)

        @c.check("react")
        async def react_check(args):
            return await self.react_check(args)

        @c.tool("voice_join")
        async def voice_join(args):
            return await self.voice_join(args)

        @c.check("voice_join")
        async def voice_join_check(args):
            return await self.voice_join_check(args)

        @c.tool("voice_leave")
        async def voice_leave(args):
            return await self.voice_leave(args)

        @c.tool("inbox")
        async def inbox(args):
            return await self.inbox(args)

        @c.on_request("deliver")
        async def deliver(body):
            return await self.deliver(body)

        @c.on_reconnect
        async def reconnected():
            await self.privacy.refresh()

        for action_id in PANEL_ACTIONS:                     # действия владельца в панели (в инструменты агента не входят)
            c.panel_action(action_id)(self._panel_handler(action_id))

    def _panel_handler(self, action_id: str):
        async def handler(args: dict) -> dict:
            return await self.panel.handle(action_id, args)
        return handler

    async def _bot(self, fn, *args):
        """Вызов бота: понятный отказ вместо сбоя (нет токена, нет подключения, нет прав, нет канала, личные слова)."""
        ok, why = self.bot.state()
        if not ok:
            raise ToolError(why, code="unavailable")
        try:
            return await fn(*args)
        except ToolError:
            raise
        except (LeakError, ChatModeError, SendDenied) as e:
            raise ToolError(str(e), code="denied") from e
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e
        except RuntimeError as e:
            raise ToolError(scrub(e, self.bot._token)[:200], code="unavailable") from e
        except Exception as e:
            raise ToolError("Discord: " + scrub(f"{type(e).__name__}: {e}", self.bot._token)[:160], code="unavailable") from e

    # ── голос ────────────────────────────────────────────────────────────────

    async def _voice(self, fn, *args, **kw):
        """Вызов голосового адаптера: понятные отказы вместо сбоев."""
        try:
            return await fn(*args, **kw)
        except ToolError:
            raise
        except VoiceError as e:
            raise ToolError(str(e), code=e.code) from e
        except ChatModeError as e:
            raise ToolError(str(e), code="denied") from e
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e
        except Exception as e:
            raise ToolError("Discord: " + scrub(f"{type(e).__name__}: {e}", self.bot._token)[:160], code="unavailable") from e

    def _voice_channel(self, args: dict):
        ok, why = self.bot.state()
        if not ok:
            raise ToolError(why, code="unavailable")
        raw = args.get("channel")
        if raw is not None and not isinstance(raw, str):
            raise ToolError("параметр «channel» должен быть строкой", code="bad_request")
        try:
            return self.voice.find_channel(raw)
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e

    async def voice_join_check(self, args: dict) -> str | None:
        """tool.check: вход в голосовой канал с прослушиванием это действие с приватностью. Канал, где сидит владелец, или тот,
        куда она уже заходила: без вопроса; иначе вопрос. Канал не найден: без вопроса (инструмент сам объяснит ошибку)."""
        try:
            channel = self.voice.find_channel(args.get("channel") if isinstance(args.get("channel"), str) else None)
        except ValueError:
            return None
        if self.voice.is_unknown(channel):
            return f"зайти в голосовой канал «{channel.guild.name} / {channel.name}» и слушать его: она там ещё не была и владельца там нет"
        return None

    async def voice_join(self, args: dict) -> str:
        channel = self._voice_channel(args)
        return await self._voice(self.voice.join, channel, by_owner=False)

    async def voice_leave(self, args: dict) -> str:
        raw = args.get("channel")
        if raw is not None and not isinstance(raw, str):
            raise ToolError("параметр «channel» должен быть строкой", code="bad_request")
        return await self._voice(self.voice.leave, raw)

    # ── inbox ────────────────────────────────────────────────────────────────

    async def inbox(self, args: dict) -> str:
        limit = _int_arg(args, "limit", 15)
        if args.get("channel"):
            return await self._bot(self.bot.read, _text_arg(args, "channel"), limit)
        if args.get("unread") is True:
            return await asyncio.to_thread(self.bot.unread, limit)
        return await asyncio.to_thread(self.bot.overview)

    # ── сообщения (инструмент message) ───────────────────────────────────────

    def _target(self, raw) -> tuple[str, str]:
        """(вид, адресат): owner или chat (название, @имя, id как их написали)."""
        text = str(raw or "").strip()
        if not text or self.bot.owner.is_target(text):
            return "owner", ""
        return "chat", text

    async def message_check(self, args: dict) -> str | None:
        """tool.check: разрешение спрашивается по АДРЕСАТУ. Владелец: без вопроса. Канал или человек: без вопроса, только если из
        него уже писали боту, иначе вопрос. Личные слова в тексте тоже поднимают вопрос."""
        kind, who = self._target(args.get("to"))
        if kind != "chat":
            return None
        text = str(args.get("text") or "")
        reason = self.privacy.reason(text) if text else None
        if reason:
            return reason
        if await self._unknown_target(who):
            return f"написать от бота Discord в «{who[:60]}», откуда ему ещё не писали"
        return None

    async def _unknown_target(self, who: str) -> bool:
        """Адресат-чат, из которого боту ещё не писали (по названию, id или «channel» при известном последнем канале)."""
        place = split_place(who)[1]
        parsed = parse_target(who)
        if parsed:                                                  # channel:<id> / dm:<id>: известен по id
            place = str(parsed[1])
        known = await asyncio.to_thread(lambda: self.bot.known.has(who) or self.bot.known.has(place)
                                        or self.bot.dialogs.last_channel() is not None and who.strip().lower() in ("channel", "канал"))
        return not known

    # ── реакции (инструмент react) ───────────────────────────────────────────

    async def react_check(self, args: dict) -> str | None:
        """tool.check: как у message, по адресату. Владелец и известный чат: без вопроса; незнакомый: вопрос."""
        kind, who = self._target(args.get("to") or "channel")
        if kind == "chat" and await self._unknown_target(who):
            return f"поставить реакцию от бота Discord в «{who[:60]}», откуда ему ещё не писали"
        return None

    async def react(self, args: dict) -> str:
        emoji = _text_arg(args, "emoji")
        mid = args.get("message_id")
        if mid is not None and (isinstance(mid, bool) or not isinstance(mid, (int, str)) or not str(mid).isdigit()):
            raise ToolError("параметр «message_id» должен быть id сообщения (число)", code="bad_request")
        remove = args.get("remove", False)
        if remove is not None and not isinstance(remove, bool):
            raise ToolError("параметр «remove» должен быть true или false", code="bad_request")
        kind, who = self._target(args.get("to") or "channel")        # без адресата: канал, откуда ей писали последним
        self.privacy.require()                                       # без списка личных слов ничего не уходит
        if kind == "chat" and self.privacy.leaks(emoji):
            raise ToolError("эмодзи не прошло проверку приватности, ничего не поставлено", code="denied")
        target = None
        if kind == "owner":
            owner = self.bot.owner.owner_id()
            if not owner:
                raise ToolError("Discord-id владельца не задан или ещё не известен (настройка «Владелец»)", code="unavailable")
            target = f"dm:{owner}"
        return await self._bot(self.bot.react, who, emoji, mid, bool(remove), target)

    async def message(self, args: dict) -> str:
        kind, who = self._target(args.get("to"))
        text = _text_arg(args, "text", required=False).strip()
        if not text:
            raise ToolError("нужен text", code="bad_request")
        reply_to = args.get("reply_to")
        if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, (int, str)) or not str(reply_to).isdigit()):
            raise ToolError("параметр «reply_to» должен быть id сообщения (число)", code="bad_request")
        self.privacy.require()                                  # без списка личных слов ничего не уходит
        if kind == "chat" and self.privacy.leaks(text):         # владелец вправе слышать своё имя
            raise ToolError("текст не прошёл проверку приватности, ничего не отправлено", code="denied")
        target = None
        if kind == "owner":
            owner = self.bot.owner.owner_id()
            if not owner:
                raise ToolError("Discord-id владельца не задан или ещё не известен (настройка «Владелец»: числовой id; имя "
                                "запоминается после его первого сообщения)", code="unavailable")
            target = f"dm:{owner}"
        sent = await self._bot(self._send, who, text, reply_to, target, kind == "chat")
        return "отправлено владельцу" if kind == "owner" else sent

    async def _send(self, who: str, text: str, reply_to, target, leak_check: bool) -> str:
        return await self.bot.send(who, text, reply_to=reply_to, target=target, leak_check=leak_check)

    async def deliver(self, body: dict) -> dict:
        """Доставить ответ ядра в Discord. target: channel:<id> (канал, ветка, пост форума), dm:<id> (личка) или owner (личка
        владельца: вопросы ядра, когда пользователь отошёл). Режим канала проверяется здесь же («чат только для чтения»)."""
        target, text = body.get("target"), body.get("text")
        if not isinstance(target, str) or not isinstance(text, str) or not text.strip():
            raise ToolError("deliver: нужны target и непустой text", code="bad_request")
        if target == "owner":
            owner = self.bot.owner.owner_id()
            if not owner:
                raise ToolError("Discord-id владельца не задан", code="unavailable")
            target = f"dm:{owner}"
        if parse_target(target) is None:
            raise ToolError(f"deliver: неизвестный адресат «{target[:60]}»", code="bad_request")
        await self._bot(self.bot.reply, target, text)
        return {"delivered": True}
