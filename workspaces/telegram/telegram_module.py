"""Модуль Telegram: сборка клиента, инструментов, доставки ответов (`deliver`) и состояния для дашборда (ТЗ 10).

`TelegramModule(client, memory_dir)` вешает обработчики на `WorkspaceClient` и создаёт фоновые задачи (`start()`):
личные слова от ядра (privacy), входящие бота-витрины (команды, подписка), цикл постов, проверка канала, её аккаунт (Telethon), состояние раз в 5 минут.
Инструменты: `message` (канал, владелец или чат с её аккаунта; текст, тема, файлы; разрешение спрашивается по адресату),
`inbox` (чтение её аккаунта), `announce` (рассылка объявлений подписчикам бота), `settings` (периодические посты и пост-итог).
Панель в дашборде (`telegram_panel.py`): вход в аккаунт по QR, список всех диалогов с режимами, открытый чат. Действия панели
(вход, выход, ручная отправка владельцем, смена режима) это `panel_action`, а не инструменты: агенту они недоступны.
Тесты собирают модуль против `gits_sdk.testing.FakeCore`, а сеть Telegram подменяют (`_call`, `_upload`, клиент Telethon).
"""
import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from common import scrub, split_target
from gits_sdk import WorkspaceClient, ToolError
from privacy import Privacy
from telegram_channel import TelegramChannel
from telegram_panel import ACTIONS as PANEL_ACTIONS
from telegram_panel import TelegramPanel
from telegram_user import ChatModeError, LeakError, TelegramUser

from gits_sdk import host
from gits_sdk.envcompat import env

log = logging.getLogger("tg_app")

logger = logging.getLogger("telegram.app")

STATE_EVERY_S = 300.0            # TG-21: состояние при изменении и раз в 5 минут
STATE_POLL_S = 5.0
PANEL_BUILD_TIMEOUT_S = 25.0     # сборка панели (диалоги, история, аватарки) дольше этого прерывается, цикл идёт дальше
VOICE_EXT = (".ogg", ".opus")

# статус публикации в канал (`TelegramChannel.ready_for_posts`) и отказ фильтра -> (текст отказа, код ошибки MOD-9)
MAX_FILES = 10
ANNOUNCE_MAX = 1000
FILE_MAX_BYTES = 50_000_000                       # лимит Telegram Bot API
FILE_KINDS = {".png": "photo", ".jpg": "photo", ".jpeg": "photo", ".webp": "photo", ".mp4": "video", ".ogg": "voice",
              ".opus": "voice"}

POST_FAILURES = {
    "not_configured": ("Telegram не настроен: нет токена бота или канала в .env", "unavailable"),
    "not_verified": ("связь с каналом не прошла проверку: токен неверный или бот не администратор канала", "unavailable"),
    "unavailable": ("список личных слов от ядра ещё не получен: публиковать пока нельзя", "unavailable"),
    "unsafe": ("текст не прошёл проверку приватности, ничего не опубликовано", "denied"),
}


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


class TelegramModule:
    def __init__(self, client: WorkspaceClient, memory_dir: str):
        self.client = client
        self.memory_dir = memory_dir
        self.privacy = Privacy(client.privacy_terms)
        self.channel = TelegramChannel(memory_dir, client, self.privacy)
        self.user = TelegramUser(memory_dir, client, self.privacy)
        self.panel = TelegramPanel(self)
        self.user.on_change = self.panel.wake.set          # вход, выход и сбой связи обновляют панель сразу
        self.tasks: list[asyncio.Task] = []
        self._panel_key: str | None = None          # последние блоки пространства, посланные ядру (TZ 15)
        self._register()

    # ── фоновые задачи ───────────────────────────────────────────────────────

    async def _privacy_loop(self) -> None:
        await self.client.wait_connected()
        await self.privacy.run()

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        for coro in (self._privacy_loop(), self.channel.run_inbox(), self.channel.run(), self.channel.run_verify(),
                     self.user.run(), self.run_state(), self._persona_loop()):
            self.tasks.append(loop.create_task(coro))
        return self.tasks

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.user.close()

    # ── состояние для дашборда (TG-21) ───────────────────────────────────────

    def _channel_word(self) -> str:
        ch = self.channel
        if not self.client.enabled:
            return "выключен"
        if not ch.configured():
            return "не настроен"
        return "готов" if ch.verified else "не проверен"

    def snapshot(self) -> tuple[str, dict]:
        """(summary, data): канал, аккаунт, последний пост, непрочитанные. Читает файлы: звать через to_thread."""
        ch, user = self.channel, self.user
        channel_word = self._channel_word()
        account = user.state_word()
        last = ch.last_post_ts()
        when = time.strftime("%d.%m %H:%M", time.localtime(last)) if last else "ещё не было"
        data = {"channel_ok": bool(ch.configured() and ch.verified), "account_ok": account == "подключён",
                "last_post_ts": last, "unread": user.unread_count()}
        return f"канал: {channel_word}, аккаунт: {account}, последний пост: {when}", data

    async def panel_blocks(self) -> list[dict]:
        """Пространство «Telegram» в дашборде (TZ 15): аккаунт (вход по QR, выход), список всех диалогов с режимами или
        открытый чат. Список диалогов и историю читает Telethon по запросу (короткие кэши), файлов памяти здесь не читаем."""
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
            # во время входа код меняется каждые ~30 с, а секунды его жизни идут в панели: обновляем чаще
            delay = min(STATE_POLL_S, 2.0) if self.user.login_active() else STATE_POLL_S
            try:
                await asyncio.wait_for(self.panel.wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            self.panel.wake.clear()

    # ── обработчики ──────────────────────────────────────────────────────────

    async def _account(self, fn, *args):
        """Вызов её аккаунта: понятный отказ вместо сбоя (нет ключей, сессии, чата, личные слова)."""
        ok, why = self.user.state()
        if not ok:
            raise ToolError(why, code="unavailable")
        try:
            return await fn(*args)
        except ToolError:
            raise
        except (LeakError, ChatModeError) as e:
            raise ToolError(str(e), code="denied") from e
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e
        except RuntimeError as e:
            raise ToolError(str(e), code="unavailable") from e
        except Exception as e:
            raise ToolError("Telegram-аккаунт: " + scrub(str(e), self.user._api_hash)[:160], code="unavailable") from e

    def _dashboard_url(self) -> str:
        """Адрес дашборда ядра: GITS_DASHBOARD_URL, иначе хост и порт ядра из адреса шины."""
        explicit = env("GITS_DASHBOARD_URL")
        if explicit:
            return explicit.rstrip("/")
        return "http://" + self.client.url.split("://", 1)[-1].split("/", 1)[0]

    async def _set_setting(self, key: str, value: bool) -> None:
        """Собственная настройка окружения (settings_schema, peers.telegram) меняется через дашборд ядра: как у питомца."""
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(f"{self._dashboard_url()}/api/peers/telegram/settings", json={"key": key, "value": value})
        except httpx.HTTPError as e:
            raise ToolError(f"ядро недоступно, настройка не сохранена: {e}", code="unavailable") from e
        if r.status_code != 200:
            raise ToolError(f"настройка не сохранена: {r.text[:160]}", code="denied")
        self.client.module_settings = {**(self.client.module_settings or {}), key: value}

    async def _voice_bytes(self, path) -> bytes | None:
        """OGG/Opus из voice_path (TG-17): только .ogg/.opus (OGG делает модуль голоса: core.tts, ТЗ 12). Другой формат, нет
        настройки или сбой: None, ответ пойдёт текстом."""
        if not path or not self.channel.flag("voice"):
            return None
        try:
            if not str(path).lower().endswith(VOICE_EXT):
                raise ValueError("нужен .ogg или .opus (перекодирования в модуле Telegram больше нет)")
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception as e:
            host.log_nowait("error", "telegram: голос для ответа не подготовлен, пойдёт текстом: " + scrub(str(e))[:160])
            return None

    async def load_persona(self) -> None:
        """Персонаж от ядра (core.persona, ТЗ 05 CHR-4): разделы персоны и алиасы. Повторно при теме character."""
        try:
            card = (await self.client.persona()).raw
        except Exception as e:
            log.warning("персона от ядра не получена: %s", e)
            return
        self.channel.set_persona(card.get("sections") or {})
        self.user.set_aliases(card.get("aliases") or [])

    async def _persona_loop(self) -> None:
        await self.client.wait_connected()
        await self.load_persona()

    def _register(self) -> None:
        c, user, privacy = self.client, self.user, self.privacy
        @c.on_topic("character")
        async def _on_character(data):
            await self.load_persona()


        # ── сообщения ──
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

        # ── чтение её аккаунта ──
        @c.tool("inbox")
        async def inbox(args):
            limit = _int_arg(args, "limit", 15)
            if args.get("chat"):
                return await self._account(user.read, _text_arg(args, "chat"), limit)
            if args.get("unread") is True:
                return await asyncio.to_thread(user.unread, limit)
            return await self._account(user.chats, _int_arg(args, "limit", 20))

        # ── объявления подписчикам бота-витрины ──
        @c.tool("announce")
        async def announce(args):
            return await self.announce(args)

        # ── настройки ──
        @c.tool("settings")
        async def settings(args):
            return await self.settings(args)

        # ── ответы ядра в Telegram (TG-17, TG-19, TG-20) ──
        @c.on_request("deliver")
        async def deliver(body):
            return await self.deliver(body)

        @c.on_reconnect
        async def reconnected():
            await privacy.refresh()

        # ── панель дашборда: действия владельца (в инструменты агента не входят) ──
        for action_id in PANEL_ACTIONS:
            c.panel_action(action_id)(self._panel_handler(action_id))

    def _panel_handler(self, action_id: str):
        async def handler(args: dict) -> dict:
            return await self.panel.handle(action_id, args)
        return handler

    async def settings(self, args: dict) -> str:
        """Периодические посты и пост-итог после забега: значения меняются через дашборд ядра; без параметров - текущие."""
        done = []
        for key, name, words in (("periodic", "publish", ("периодические посты в Telegram включены",
                                                          "периодические посты в Telegram выключены")),
                                 ("after_run", "after_run", ("после каждого забега (гибели колонии) будет писать пост-итог в канал",
                                                            "постов-итогов после забега больше не будет"))):
            value = args.get(key)
            if value is None:
                continue
            if not isinstance(value, bool):
                raise ToolError(f"параметр «{key}» должен быть true или false", code="bad_request")
            await self._set_setting(name, value)
            done.append(words[0] if value else words[1])
        if done:
            return "; ".join(done)
        ch = self.channel
        return (f"периодические посты: {'включены' if ch.flag('publish') else 'выключены'}; "
                f"пост-итог после забега: {'включён' if ch.flag('after_run') else 'выключен'}")

    # ── объявления (инструмент announce) ──

    async def announce(self, args: dict) -> str:
        """Рассылка текста (и необязательной ссылки) всем подписчикам бота. Текст задаёт вызывающий; личные слова из списка
        ядра в него не пропускаются (рассылка публичная)."""
        text = _text_arg(args, "text").strip()
        url = _text_arg(args, "url", required=False).strip()
        if len(text) > ANNOUNCE_MAX:
            raise ToolError(f"текст объявления не длиннее {ANNOUNCE_MAX} символов", code="bad_request")
        if url and not url.lower().startswith(("http://", "https://")):
            raise ToolError("параметр «url» должен быть ссылкой http или https", code="bad_request")
        self.privacy.require()
        if self.privacy.leaks(text):
            raise ToolError(POST_FAILURES["unsafe"][0].replace("опубликовано", "разослано"), code="denied")
        if not self.channel.has_token():
            raise ToolError("бот не настроен: нет TELEGRAM_BOT_TOKEN в .env", code="unavailable")
        if not await asyncio.to_thread(self.channel.subscribers.count):
            return "подписчиков пока нет, объявление никому не отправлено"
        try:
            res = await self.channel.broadcast(text, url)
        except RuntimeError as e:
            raise ToolError(scrub(str(e), self.channel._token)[:160], code="unavailable") from e
        out = f"объявление отправлено: {res['sent']} из {res['total']}"
        if res["removed"]:
            out += f"; снято с подписки (заблокировали бота): {res['removed']}"
        if res["failed"]:
            out += f"; не дошло из-за сбоев: {res['failed']}"
        return out

    # ── сообщения (инструмент message) ───────────────────────────────────────

    def _target(self, raw) -> tuple[str, str]:
        """(вид, адресат): channel, owner или chat (название, @имя или id как их написали)."""
        text = str(raw or "").strip()
        if text.startswith("user:"):
            text = text[5:]                                  # адрес из события (`reply_channel.target`: `user:<чат>[#тема]`)
        low = text.lstrip("@").lower()
        if low in ("", "channel", "канал"):
            return "channel", ""
        chan = self.channel._channel.lstrip("@").lower()
        if chan and low == chan:
            return "channel", ""
        if self.channel.owner.is_target(text):
            return "owner", ""
        return "chat", text

    async def message_check(self, args: dict) -> str | None:
        """tool.check: разрешение спрашивается по АДРЕСАТУ. Канал и владелец: без вопроса. Чат с её личного аккаунта: без
        вопроса, только если из него уже писали (бот или аккаунт), иначе вопрос. Личные слова в тексте тоже поднимают вопрос."""
        kind, who = self._target(args.get("to"))
        if kind != "chat":
            return None
        text = str(args.get("text") or "")
        reason = self.privacy.reason(text) if text else None
        if reason:
            return reason
        who = split_target(who)[0]                                  # «Группа / Тема»: известность считается по группе
        known = await asyncio.to_thread(lambda: self.channel.known.has(who) or self.user.known.has(who))
        if not known:
            return f"написать с её личного аккаунта в чат «{who[:60]}», из которого ей ещё не писали"
        return None

    def _resolve_files(self, names) -> list[tuple[Path, str]]:
        """files -> [(путь, вид)]: снимок (имя или last) из папки снимков либо идентификатор из core.resources."""
        from gits_sdk.resources import shared_file

        if names is None:
            return []
        if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
            raise ToolError("параметр «files» должен быть списком строк", code="bad_request")
        if len(names) > MAX_FILES:
            raise ToolError(f"файлов не больше {MAX_FILES}", code="bad_request")
        out = []
        for name in names:
            name = name.strip()
            path = self.channel.shots.last() if name == "last" else self.channel.shots.resolve(name)
            if path is None:
                path = shared_file(name)
            if path is None:
                raise ToolError(f"файла «{name[:80]}» нет среди снимков и ресурсов ядра", code="bad_request")
            kind = FILE_KINDS.get(path.suffix.lower(), "document")
            if path.stat().st_size > FILE_MAX_BYTES:
                raise ToolError(f"файл «{path.name}» больше лимита Telegram Bot API (50 МБ)", code="bad_request")
            out.append((path, kind))
        return out

    async def message(self, args: dict) -> str:
        ch, privacy = self.channel, self.privacy
        if args.get("after_run") and not ch.flag("after_run"):
            return "пост-итог после забега выключен в настройках Telegram"
        kind, who = self._target(args.get("to"))
        text = _text_arg(args, "text", required=False).strip()
        topic = _text_arg(args, "topic", required=False).strip()
        reply_to = args.get("reply_to")
        if reply_to is not None and (isinstance(reply_to, bool) or not isinstance(reply_to, int)):
            raise ToolError("параметр «reply_to» должен быть числом", code="bad_request")
        files = await asyncio.to_thread(self._resolve_files, args.get("files"))
        if not text and not topic and not files:
            raise ToolError("нужен text, topic или files", code="bad_request")
        if kind == "channel":
            status = await ch.ready_for_posts()
            if status:
                msg, code = POST_FAILURES[status]
                raise ToolError(msg, code=code)
        privacy.require()                                  # TG-5: без списка личных слов ничего не уходит
        if not text and topic:
            text = await ch.compose_post(topic) or ""
            if not text:
                raise ToolError(POST_FAILURES["unsafe"][0], code="denied")
        # фильтр приватности на каждый исходящий текст: канал строже всего (ссылки, @упоминания, длина), чат - личные слова;
        # владелец вправе слышать своё имя
        if text and kind == "channel" and ch._safe(text) is None:
            raise ToolError(POST_FAILURES["unsafe"][0], code="denied")
        if text and kind == "chat" and privacy.leaks(text):
            raise ToolError(POST_FAILURES["unsafe"][0], code="denied")
        voice = args.get("voice")
        voice = (ch.flag("voice") if kind == "channel" else False) if voice is None else bool(voice)
        try:
            return await self._send_message(kind, who, text, files, voice, reply_to)
        except ToolError:
            raise
        except (LeakError, ChatModeError) as e:
            raise ToolError(str(e), code="denied") from e
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e
        except Exception as e:
            raise ToolError("не удалось отправить в Telegram: " + scrub(str(e), ch._token)[:160], code="unavailable") from e

    async def _send_message(self, kind: str, who: str, text: str, files: list, voice: bool, reply_to) -> str:
        ch = self.channel
        if kind == "chat":
            ok, why = self.user.state()
            if not ok:
                raise ToolError(why, code="unavailable")
            if files:
                return await self.user.send_files(who, files, text, reply_to)
            ogg = await ch.try_ogg(text) if voice else None
            return await self.user.send(who, text, ogg, reply_to)
        chat_id = ch._channel
        if kind == "owner":
            chat_id = ch.owner_id()
            if not chat_id:
                raise ToolError("Telegram-аккаунт владельца не задан в настройках (поле «Аккаунт владельца»)", code="unavailable")
        if files:
            await ch.send_files(chat_id, files, "" if (voice and text) else text, reply_to)
            if voice and text:
                await ch.reply(chat_id, text, await ch.try_ogg(text))
        else:
            ogg = await ch.try_ogg(text) if voice else None
            await ch.reply(chat_id, text, ogg, reply_to)
        if kind == "owner":
            return "отправлено владельцу"
        await ch._record_post(text, "опубликовано")
        return "опубликован пост" + (f": «{text}»" if text else f" (файлов: {len(files)})")

    async def deliver(self, body: dict) -> dict:
        """Доставить ответ ядра в Telegram. target: bot:<chat_id> - от бота, user:<chat_id> - с её аккаунта (в теме форума
        user:<chat_id>#<id темы>: ответ уходит в ту же тему, режим берётся у темы), owner - личка владельца от бота (вопросы
        ядра, когда пользователь отошёл)."""
        target, text = body.get("target"), body.get("text")
        if not isinstance(target, str) or not isinstance(text, str) or not text.strip():
            raise ToolError("deliver: нужны target и непустой text", code="bad_request")
        if target == "owner":
            owner = self.channel.owner_id()
            if not owner:
                raise ToolError("Telegram-id владельца не задан", code="unavailable")        # TG-20
            target = f"bot:{owner}"
        kind, _, ident = target.partition(":")
        ident, _, topic_raw = ident.partition("#")
        if kind not in ("bot", "user") or not ident.lstrip("-").isdigit() or (topic_raw and not (
                topic_raw.isdigit() and int(topic_raw) > 0 and kind == "user")):
            raise ToolError(f"deliver: неизвестный адресат «{target}»", code="bad_request")
        chat_id, topic = int(ident), (int(topic_raw) if topic_raw else None)
        ogg = await self._voice_bytes(body.get("voice_path"))
        try:
            if kind == "bot":
                await self.channel.reply(chat_id, text, ogg)
            else:
                ok, why = self.user.state()
                if not ok:
                    raise ToolError(why, code="unavailable")
                await self.user.reply(chat_id, text, ogg, topic)
        except ToolError:
            raise
        except ChatModeError as e:
            raise ToolError(str(e), code="denied") from e
        except Exception as e:
            raise ToolError("не удалось доставить в Telegram: " + scrub(str(e), self.channel._token)[:160],
                            code="unavailable") from e
        return {"delivered": True}
