"""Модуль Twitch: сборка клиента, события чата и канала, инструменты, доставка ответов (`deliver`), состояние.

`TwitchModule(client, memory_dir)` вешает обработчики на `ModuleClient` и создаёт фоновые задачи (`start()`): личные слова от
ядра (privacy), сессии EventSub (бот: чат; владелец: события канала, если он вошёл), состояние раз в 5 минут. Тесты собирают
модуль против `gits_sdk.testing.FakeCore`, а сеть Twitch подменяют (`Helix._request`, `EventSubSession._open`).
Тексты зрителей это данные: в событие они идут как факт «сообщение от X: «...»», инструкций оттуда никто не выполняет.
"""
import asyncio
import logging
import os
import time
from collections import deque

from common import chunks, message_fact, quiet_active, scrub, sender_info
from eventsub import EventSubSession
from gits_sdk import ModuleClient, ToolError
from privacy import Privacy
from twitch_api import Helix, Tokens, TwitchError

log = logging.getLogger("twitch.app")

STATE_EVERY_S = 300.0
STATE_POLL_S = 5.0
RECENT_MAX = 50


def _text_arg(args: dict, name: str, required: bool = True) -> str:
    value = args.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"параметр «{name}» должен быть непустой строкой", code="bad_request")
    return value.strip()


def _int_arg(args: dict, name: str, default: int) -> int:
    value = args.get(name)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"параметр «{name}» должен быть числом", code="bad_request")
    return int(value)


class TwitchModule:
    def __init__(self, client: ModuleClient, memory_dir: str):
        self.client = client
        self.memory_dir = memory_dir
        self.privacy = Privacy(client.privacy_terms)
        self.tokens = Tokens(memory_dir)
        self.helix = Helix(self.tokens, os.getenv("TWITCH_CLIENT_ID", "").strip(), os.getenv("TWITCH_CLIENT_SECRET", "").strip())
        self.channel_login = os.getenv("TWITCH_CHANNEL", "").strip().lstrip("@").lower()
        self.channel_id = ""
        self.recent: deque[str] = deque(maxlen=RECENT_MAX)
        self.live: bool | None = None
        self.sessions: list[EventSubSession] = []
        self.tasks: list[asyncio.Task] = []
        self._minute = (0, 0)                       # (минута, сколько обычных сообщений чата уже отдано)
        self._register()

    # ── настройка ────────────────────────────────────────────────────────────

    def configured(self) -> bool:
        return self.helix.configured() and self.helix.has_role("bot") and bool(self.channel_login)

    def _cfg(self, name: str, default):
        return self.client.module_settings.get(name, default)

    def _owner(self, login: str) -> bool:
        owner = str(self._cfg("owner_login", "") or self.channel_login).lower().lstrip("@")
        return bool(owner) and login.lower() == owner

    async def resolve_channel(self) -> str:
        if not self.channel_id:
            if self.helix.has_role("broadcaster") and self.helix.login("broadcaster") == self.channel_login:
                self.channel_id = self.helix.user_id("broadcaster")
            else:
                self.channel_id = str((await self.helix.lookup(self.channel_login)).get("id") or "")
        return self.channel_id

    def _need_ready(self) -> None:
        if not self.configured():
            raise ToolError("Twitch не настроен: нужны TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_CHANNEL в "
                            ".env и вход бота (tw_login.py)", code="unavailable")

    # ── фон ──────────────────────────────────────────────────────────────────

    async def _privacy_loop(self) -> None:
        await self.client.wait_connected()
        await self.privacy.run()

    def _subs(self, role: str, cid: str) -> list[tuple[str, str, dict]]:
        bot = self.helix.user_id("bot")
        if role == "bot":
            return [("channel.chat.message", "1", {"broadcaster_user_id": cid, "user_id": bot}),
                    ("stream.online", "1", {"broadcaster_user_id": cid}),
                    ("stream.offline", "1", {"broadcaster_user_id": cid})]
        return [("channel.follow", "2", {"broadcaster_user_id": cid, "moderator_user_id": cid}),
                ("channel.subscribe", "1", {"broadcaster_user_id": cid}),
                ("channel.subscription.gift", "1", {"broadcaster_user_id": cid}),
                ("channel.cheer", "1", {"broadcaster_user_id": cid}),
                ("channel.raid", "1", {"to_broadcaster_user_id": cid}),
                ("channel.channel_points_custom_reward_redemption.add", "1", {"broadcaster_user_id": cid})]

    async def _eventsub_loop(self) -> None:
        await self.client.wait_connected()
        while not self.configured():
            await asyncio.sleep(30)                              # без настройки модуль живёт и честно пишет «не настроен»
            self.tokens.load()
            self.helix.client_id = self.helix.client_id or os.getenv("TWITCH_CLIENT_ID", "").strip()
        try:
            cid = await self.resolve_channel()
        except TwitchError as e:
            log.warning("канал не найден: %s", scrub(e.message))
            return
        roles = ["bot"] + (["broadcaster"] if self.helix.has_role("broadcaster") else [])
        for role in roles:
            session = EventSubSession(self.helix, role, self._subs(role, cid), self.on_event)
            self.sessions.append(session)
            self.tasks.append(asyncio.get_running_loop().create_task(session.run()))

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        for coro in (self._privacy_loop(), self._eventsub_loop(), self.run_state()):
            self.tasks.append(loop.create_task(coro))
        return self.tasks

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.helix.close()

    # ── события ──────────────────────────────────────────────────────────────

    def _note(self, text: str) -> None:
        self.recent.append(f"{time.strftime('%H:%M')} {text}")

    def _chat_budget(self) -> bool:
        limit = int(self._cfg("chat_events_per_min", 20) or 0)
        minute = int(time.time() // 60)
        m, used = self._minute
        used = used if m == minute else 0
        if used >= limit:
            self._minute = (minute, used)
            return False
        self._minute = (minute, used + 1)
        return True

    async def on_event(self, sub_type: str, ev: dict) -> bool:
        """Одно уведомление EventSub -> событие ядру. Возвращает, отправлено ли оно."""
        if quiet_active(self.client.settings):
            pass                                      # событие всё равно уходит: тишину соблюдает ядро (речь и срочность)
        name = ev.get("user_name") or ev.get("chatter_user_name") or ev.get("from_broadcaster_user_name") or "зритель"
        if sub_type == "channel.chat.message":
            return await self._on_chat(ev)
        if sub_type == "channel.follow":
            self._note(f"фолловер {name}")
            return await self.client.event("twitch.follow", f"Новый фолловер на Twitch: {name}.", memorable=False,
                                           sender=sender_info(str(ev.get("user_id") or ""), name, False),
                                           dedupe_key=f"tw:follow:{ev.get('user_id')}:{ev.get('followed_at')}")
        if sub_type in ("channel.subscribe", "channel.subscription.gift"):
            gift = sub_type.endswith("gift")
            text = (f"{name} подарил(а) подписок: {ev.get('total', 1)}." if gift else f"{name} подписался(лась) на канал (тир {ev.get('tier')}).")
            self._note(text)
            return await self.client.event("twitch.subscribe", text, data={"gift": gift, "tier": ev.get("tier")})
        if sub_type == "channel.cheer":
            text = f"{name if not ev.get('is_anonymous') else 'Аноним'} прислал(а) {ev.get('bits')} бит: «{str(ev.get('message') or '')[:200]}»"
            self._note(text)
            return await self.client.event("twitch.cheer", text, data={"bits": ev.get("bits")})
        if sub_type == "channel.raid":
            text = f"Рейд от {name}: {ev.get('viewers')} зрителей."
            self._note(text)
            return await self.client.event("twitch.raid", text, data={"viewers": ev.get("viewers")})
        if sub_type.startswith("channel.channel_points_custom_reward_redemption"):
            reward = (ev.get("reward") or {}).get("title") or "награда"
            text = f"{name} получил(а) награду «{reward}»: «{str(ev.get('user_input') or '')[:200]}»".replace(": «»", "")
            self._note(text)
            return await self.client.event("twitch.redemption", text, data={"reward": reward, "id": ev.get("id")},
                                           sender=sender_info(str(ev.get("user_id") or ""), name, False))
        if sub_type == "stream.online":
            self.live = True
            self._note("трансляция началась")
            return await self.client.event("twitch.stream_online", "Трансляция на Twitch началась.", announce=False)
        if sub_type == "stream.offline":
            self.live = False
            self._note("трансляция закончилась")
            return await self.client.event("twitch.stream_offline", "Трансляция на Twitch закончилась.")
        return False

    async def _on_chat(self, ev: dict) -> bool:
        login = str(ev.get("chatter_user_login") or "").lower()
        if login == self.helix.login("bot").lower():
            return False                                          # собственные сообщения не события
        text = str((ev.get("message") or {}).get("text") or "")
        if not text.strip():
            return False
        name = str(ev.get("chatter_user_name") or login or "зритель")
        owner = self._owner(login)
        bot_login = self.helix.login("bot").lower()
        reply_to_me = str((ev.get("reply") or {}).get("parent_user_login") or "").lower() == bot_login
        mention = reply_to_me or (bool(bot_login) and f"@{bot_login}" in text.lower())
        if not (mention or owner or self._chat_budget()):
            return False
        mid = str(ev.get("message_id") or "")
        self._note(f"{name}: {text[:80]}")
        return bool(await self.client.event(
            "twitch.mention" if mention else "twitch.chat", message_fact(name, text),
            addressed_to_me=mention or owner, sender=sender_info(str(ev.get("chatter_user_id") or ""), name, owner),
            reply_channel={"module": "twitch", "target": f"chat:{mid}"}, dedupe_key=f"tw:chat:{mid}",
            memorable=mention or owner, data={"message_id": mid, "login": login}))

    # ── инструменты ──────────────────────────────────────────────────────────

    def _api_error(self, e: TwitchError) -> ToolError:
        code = "denied" if e.status in (401, 403) else "bad_request" if e.status in (400, 404, 422) else "unavailable"
        return ToolError(scrub(e.message, self.helix.client_secret)[:200], code=code)

    async def _say(self, text: str, reply_to: str = "") -> int:
        self._need_ready()
        self.privacy.require()
        if self.privacy.leaks(text):
            raise ToolError("текст не прошёл проверку приватности, в чат ничего не отправлено", code="denied")
        cid = await self.resolve_channel()
        sent = 0
        try:
            for part in chunks(text):
                await self.helix.send_chat(cid, part, reply_to if sent == 0 else "")
                sent += 1
        except TwitchError as e:
            raise self._api_error(e) from e
        return sent

    def _register(self) -> None:
        c = self.client
        privacy = self.privacy

        @c.tool("chat_say")
        async def chat_say(args):
            await self._say(_text_arg(args, "text"))
            return {"sent": True}

        @c.check("chat_say")
        async def chat_say_check(args):
            return privacy.reason(str(args.get("text") or "")) if privacy.ready else None

        @c.tool("announce")
        async def announce(args):
            text = _text_arg(args, "text")
            self._need_ready()
            privacy.require()
            if privacy.leaks(text):
                raise ToolError("текст не прошёл проверку приватности, ничего не отправлено", code="denied")
            try:
                await self.helix.announce(await self.resolve_channel(), text[:500])
            except TwitchError as e:
                raise self._api_error(e) from e
            return {"sent": True}

        c.check("announce")(chat_say_check)

        @c.tool("stream_status")
        async def stream_status(args):
            self._need_ready()
            try:
                cid = await self.resolve_channel()
                stream, info = await self.helix.stream(cid), await self.helix.channel_info(cid)
            except TwitchError as e:
                raise self._api_error(e) from e
            self.live = bool(stream)
            return {"live": bool(stream), "title": (stream or info).get("title") or info.get("title", ""),
                    "game": (stream or info).get("game_name") or info.get("game_name", ""),
                    "viewers": (stream or {}).get("viewer_count", 0)}

        @c.tool("set_stream")
        async def set_stream(args):
            self._need_ready()
            title, game = _text_arg(args, "title", False), _text_arg(args, "game", False)
            if not (title or game):
                raise ToolError("нужно название или игра", code="bad_request")
            if not self.helix.has_role("broadcaster"):
                raise ToolError("для смены названия нужен вход владельца канала (tw_login.py broadcaster)", code="unavailable")
            if privacy.ready and privacy.leaks(f"{title} {game}"):
                raise ToolError("текст не прошёл проверку приватности", code="denied")
            try:
                return {"changed": await self.helix.set_channel(await self.resolve_channel(), title[:140], game)}
            except TwitchError as e:
                raise self._api_error(e) from e

        c.check("set_stream")(chat_say_check_for("title", privacy))

        @c.tool("clip")
        async def clip(args):
            self._need_ready()
            try:
                made = await self.helix.clip(await self.resolve_channel())
            except TwitchError as e:
                raise self._api_error(e) from e
            return {"clip_id": made.get("id"), "edit_url": made.get("edit_url")}

        @c.tool("recent_events")
        async def recent_events(args):
            limit = max(1, min(_int_arg(args, "limit", 15), RECENT_MAX))
            return {"events": list(self.recent)[-limit:]}

        @c.tool("timeout_user")
        async def timeout_user(args):
            return await self._moderate(args, seconds=max(1, min(_int_arg(args, "seconds", 300), 1209600)))

        @c.tool("ban_user")
        async def ban_user(args):
            return await self._moderate(args, seconds=0)

        @c.tool("delete_message")
        async def delete_message(args):
            self._need_ready()
            try:
                await self.helix.delete_message(await self.resolve_channel(), _text_arg(args, "message_id"))
            except TwitchError as e:
                raise self._api_error(e) from e
            return {"deleted": True}

        @c.on_request("deliver")
        async def deliver(body):
            return await self.deliver(body)

        @c.on_reconnect
        async def reconnected():
            await privacy.refresh()

    async def _moderate(self, args: dict, seconds: int) -> dict:
        self._need_ready()
        login = _text_arg(args, "user")
        reason = _text_arg(args, "reason", False)
        try:
            cid = await self.resolve_channel()
            target = await self.helix.lookup(login)
            if str(target.get("id")) in (cid, self.helix.user_id("bot")):
                raise ToolError("себя и владельца канала модерировать нельзя", code="denied")
            await self.helix.ban(cid, str(target["id"]), seconds, reason)
        except TwitchError as e:
            raise self._api_error(e) from e
        return {"done": True, "user": login, "seconds": seconds}

    async def deliver(self, body: dict) -> dict:
        """Ответ ядра в чат. target: chat:<id сообщения> - ответом на сообщение, chat - просто в чат."""
        target, text = body.get("target"), body.get("text")
        if not isinstance(target, str) or not isinstance(text, str) or not text.strip():
            raise ToolError("deliver: нужны target и непустой text", code="bad_request")
        kind, _, ident = target.partition(":")
        if kind != "chat":
            raise ToolError(f"deliver: неизвестный адресат «{target}»", code="bad_request")
        await self._say(text, ident)
        return {"delivered": True}

    # ── состояние ────────────────────────────────────────────────────────────

    def snapshot(self) -> tuple[str, dict]:
        if not self.client.enabled:
            word = "выключен"
        elif not self.configured():
            word = "не настроен"
        else:
            word = "подключён" if any(s.connected for s in self.sessions) else "подключаюсь"
        live = {True: "в эфире", False: "не в эфире", None: "эфир неизвестен"}[self.live]
        failed = sorted({t for s in self.sessions for t in s.failed})
        return f"Twitch: {word}, {live}", {"ready": word == "подключён", "live": self.live, "failed_subscriptions": failed}

    async def run_state(self) -> None:
        last_key, sent_at = None, 0.0
        while True:
            try:
                summary, data = self.snapshot()
                key = (summary, tuple(data["failed_subscriptions"]))
                if key != last_key or time.time() - sent_at >= STATE_EVERY_S:
                    await self.client.state(summary, data)
                    last_key, sent_at = key, time.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("состояние не отправлено: %s", scrub(str(e)))
            await asyncio.sleep(STATE_POLL_S)


def chat_say_check_for(field: str, privacy):
    async def check(args):
        return privacy.reason(f"{args.get('title') or ''} {args.get('game') or ''}") if privacy.ready else None
    return check
