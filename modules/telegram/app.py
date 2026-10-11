"""Модуль Telegram: сборка клиента, инструментов, доставки ответов (`deliver`) и состояния для дашборда (ТЗ 10).

`TelegramModule(client, memory_dir)` вешает обработчики на `ModuleClient` и создаёт фоновые задачи (`start()`):
личные слова от ядра (privacy), входящие бота, цикл постов, проверка канала, её аккаунт (Telethon), состояние раз в 5 минут.
Тесты собирают модуль против `glados_module.testing.FakeCore`, а сеть Telegram подменяют (`_call`, `_upload`, клиент Telethon).
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from common import scrub
from glados_module import ModuleClient, ToolError
from privacy import Privacy
from telegram_channel import TelegramChannel
from telegram_user import LeakError, TelegramUser

import events_log

logger = logging.getLogger("telegram.app")

STATE_EVERY_S = 300.0            # TG-21: состояние при изменении и раз в 5 минут
STATE_POLL_S = 5.0
VOICE_EXT = (".ogg", ".opus")

# статус публикации (telegram_channel.publish / post_on_request) -> (текст отказа, код ошибки MOD-9)
POST_FAILURES = {
    "not_configured": ("Telegram не настроен: нет токена бота или канала в agent/secrets.env", "unavailable"),
    "not_verified": ("связь с каналом не прошла проверку: токен неверный или бот не администратор канала", "unavailable"),
    "unavailable": ("список личных слов от ядра ещё не получен: публиковать пока нельзя", "unavailable"),
    "unsafe": ("текст не прошёл проверку приватности, ничего не опубликовано", "denied"),
    "declined": ("пользователь не разрешил публикацию", "denied"),
    "failed": ("Telegram вернул ошибку, пост не опубликован", "unavailable"),
    "empty": ("черновик пуст: ни текста, ни снимков", "bad_request"),
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
    def __init__(self, client: ModuleClient, memory_dir: str):
        self.client = client
        self.memory_dir = memory_dir
        self.privacy = Privacy(client.request)
        self.channel = TelegramChannel(memory_dir, client, self.privacy)
        self.user = TelegramUser(memory_dir, client, self.privacy)
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
                     self.user.run(), self.run_state()):
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

    def panel_blocks(self) -> list[dict]:
        """Пространство «Telegram» в дашборде (TZ 15, TAB-9): канал, аккаунт, непрочитанные, последний пост и последние
        диалоги из входящих. Без текста сообщений: только имя чата, отправитель и время. Читает файлы: через to_thread."""
        account = self.user.state_word()
        last = self.channel.last_post_ts()
        when = time.strftime("%d.%m %H:%M", time.localtime(last)) if last else "ещё не было"
        seen, rows = set(), []
        for rec in reversed(self.user._load_inbox()):             # свежие сверху, один диалог - одна строка
            chat = str(rec.get("chat") or "")[:120]
            if chat in seen:
                continue
            seen.add(chat)
            stamp = time.strftime("%d.%m %H:%M", time.localtime(rec.get("ts", 0)))
            rows.append({"text": chat or "без названия", "sub": f"{str(rec.get('from') or '')[:80]}, {stamp}"})
            if len(rows) >= 8:
                break
        return [
            {"type": "stats", "title": "Сейчас", "items": [
                {"label": "Канал", "value": self._channel_word()},
                {"label": "Аккаунт", "value": account},
                {"label": "Непрочитано", "value": self.user.unread_count()},
                {"label": "Последний пост", "value": when}]},
            {"type": "list", "title": "Последние диалоги", "items": rows} if rows
            else {"type": "text", "title": "Последние диалоги", "text": "Входящих в её аккаунт пока не было."},
        ]

    async def run_state(self) -> None:
        last_key, sent_at = None, 0.0
        while True:
            try:
                summary, data = await asyncio.to_thread(self.snapshot)
                key = (summary, json.dumps(data, sort_keys=True))
                if key != last_key or time.monotonic() - sent_at >= STATE_EVERY_S:
                    await self.client.state(summary, data)
                    last_key, sent_at = key, time.monotonic()
                blocks = await asyncio.to_thread(self.panel_blocks)
                pkey = json.dumps(blocks, ensure_ascii=False, sort_keys=True)
                if pkey != self._panel_key and await self.client.panel.set(blocks):
                    self._panel_key = pkey
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("состояние не отправлено: %s", e)
            await asyncio.sleep(STATE_POLL_S)

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
        except LeakError as e:
            raise ToolError(str(e), code="denied") from e
        except ValueError as e:
            raise ToolError(str(e), code="bad_request") from e
        except RuntimeError as e:
            raise ToolError(str(e), code="unavailable") from e
        except Exception as e:
            raise ToolError("Telegram-аккаунт: " + scrub(str(e), self.user._api_hash)[:160], code="unavailable") from e

    async def _set_setting(self, key: str, value: bool) -> None:
        """TG-2/TG-13: общая настройка меняется встроенным инструментом ядра core.set_setting."""
        await self.client.request("core.tool.call", {"tool": "core.set_setting", "args": {"key": key, "value": value}},
                                  timeout=30)

    async def _voice_bytes(self, path) -> bytes | None:
        """OGG/Opus из voice_path (TG-17): только .ogg/.opus (OGG делает модуль голоса: core.tts, ТЗ 12). Другой формат, нет
        настройки или сбой: None, ответ пойдёт текстом."""
        if not path or not self.client.settings.get("telegram_voice"):
            return None
        try:
            if not str(path).lower().endswith(VOICE_EXT):
                raise ValueError("нужен .ogg или .opus (перекодирования в модуле Telegram больше нет)")
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception as e:
            events_log.log_nowait("error", "telegram: голос для ответа не подготовлен, пойдёт текстом: " + scrub(str(e))[:160])
            return None

    def _register(self) -> None:
        c, ch, user, privacy = self.client, self.channel, self.user, self.privacy

        # ── публикации ──
        @c.tool("post")
        async def post(args):
            status, text = await ch.post_on_request(args.get("topic") or "приветственный пост", args.get("voice"))
            if status != "ok":
                msg, code = POST_FAILURES.get(status, ("публикация не удалась", "unavailable"))
                raise ToolError(msg, code=code)
            return f"опубликован пост: «{text}»"

        @c.tool("draft_show")
        async def draft_show(args):
            return await asyncio.to_thread(ch.draft.preview)

        @c.tool("draft_write")
        async def draft_write(args):
            text = _text_arg(args, "text", required=False).strip()
            if not text:
                privacy.require()
                text = await ch.compose(args.get("topic") or "приветственный пост", (await asyncio.to_thread(ch.draft.get))["text"])
            if not text:
                raise ToolError("текст не получился или не прошёл проверку приватности", code="unavailable")
            await asyncio.to_thread(ch.draft.set_text, text)
            ch.log("черновик написан: " + text)
            return "черновик написан:\n" + text

        @c.tool("draft_append")
        async def draft_append(args):
            text = _text_arg(args, "text", required=False).strip()
            if not text:
                raise ToolError("нечего добавлять: текст пуст", code="bad_request")
            await asyncio.to_thread(ch.draft.append, text)
            return "добавлено в конец черновика"

        @c.tool("draft_edit")
        async def draft_edit(args):
            current = (await asyncio.to_thread(ch.draft.get))["text"]
            if not current:
                raise ToolError("черновик пуст, сначала его нужно написать", code="bad_request")
            privacy.require()
            text = await ch.compose(_text_arg(args, "instruction"), current)
            if not text:
                raise ToolError("правка не получилась или не прошла проверку приватности, черновик не изменён", code="unavailable")
            await asyncio.to_thread(ch.draft.set_text, text)
            ch.log("черновик отредактирован: " + text)
            return "черновик переписан:\n" + text

        @c.tool("draft_replace")
        async def draft_replace(args):
            if not await asyncio.to_thread(ch.draft.replace, _text_arg(args, "find"), _text_arg(args, "replace", required=False)):
                raise ToolError("такого фрагмента в черновике нет", code="bad_request")
            return "заменено"

        @c.tool("draft_attach")
        async def draft_attach(args):
            try:
                name = await asyncio.to_thread(ch.draft.attach, args.get("shot") or "last")
            except ValueError as e:
                raise ToolError(str(e), code="bad_request") from e
            return f"снимок {name} приложен к черновику"

        @c.tool("draft_detach")
        async def draft_detach(args):
            n = await asyncio.to_thread(ch.draft.detach, args.get("shot") or "all")
            return f"снято снимков: {n}"

        @c.tool("draft_clear")
        async def draft_clear(args):
            await asyncio.to_thread(ch.draft.clear)
            return "черновик очищен"

        @c.tool("draft_publish")
        async def draft_publish(args):
            d = await asyncio.to_thread(ch.draft.get)
            photos = [p for p in (ch.draft.shots.resolve(n) for n in d["photos"]) if p]
            status, text = await ch.publish(d["text"], photos, args.get("voice"))
            if status != "ok":
                msg, code = POST_FAILURES.get(status, ("публикация не удалась", "unavailable"))
                raise ToolError(msg, code=code)
            await asyncio.to_thread(ch.draft.clear)
            return "опубликовано из черновика" + (f" (со снимками: {len(photos)})" if photos else "") + (f": «{text}»" if text else "")

        # ── её аккаунт ──
        @c.tool("chats")
        async def chats(args):
            return await self._account(user.chats, _int_arg(args, "limit", 20))

        @c.tool("read")
        async def read(args):
            return await self._account(user.read, _text_arg(args, "chat"), _int_arg(args, "limit", 15))

        @c.tool("unread")
        async def unread(args):
            return await asyncio.to_thread(user.unread, _int_arg(args, "limit", 15))

        @c.tool("send")
        async def send(args):
            privacy.require()
            return await self._account(user.send, _text_arg(args, "chat"), _text_arg(args, "text"))

        @c.check("send")
        async def send_check(args):
            return privacy.reason(str(args.get("text") or ""))

        # ── настройки ──
        @c.tool("set_periodic")
        async def set_periodic(args):
            enabled = self._flag(args)
            await self._set_setting("telegram", enabled)
            return ("периодические посты в Telegram включены" if enabled else "периодические посты в Telegram выключены")

        @c.tool("set_after_run")
        async def set_after_run(args):
            enabled = self._flag(args)
            await self._set_setting("telegram_after_run", enabled)
            return ("после каждого забега (гибели колонии) будет писать пост-итог в канал" if enabled
                    else "постов-итогов после забега больше не будет")

        # ── ответы ядра в Telegram (TG-17, TG-19, TG-20) ──
        @c.on_request("deliver")
        async def deliver(body):
            return await self.deliver(body)

        @c.on_reconnect
        async def reconnected():
            await privacy.refresh()

    @staticmethod
    def _flag(args: dict) -> bool:
        value = args.get("enabled")
        if not isinstance(value, bool):
            raise ToolError("параметр «enabled» должен быть true или false", code="bad_request")
        return value

    async def deliver(self, body: dict) -> dict:
        """Доставить ответ ядра в Telegram. target: bot:<chat_id> - от бота, user:<chat_id> - с её аккаунта, owner - личка
        владельца от бота (вопросы ядра, когда пользователь отошёл)."""
        target, text = body.get("target"), body.get("text")
        if not isinstance(target, str) or not isinstance(text, str) or not text.strip():
            raise ToolError("deliver: нужны target и непустой text", code="bad_request")
        if target == "owner":
            owner = self.channel.owner_id()
            if not owner:
                raise ToolError("Telegram-id владельца не задан", code="unavailable")        # TG-20
            target = f"bot:{owner}"
        kind, _, ident = target.partition(":")
        if kind not in ("bot", "user") or not ident.lstrip("-").isdigit():
            raise ToolError(f"deliver: неизвестный адресат «{target}»", code="bad_request")
        chat_id = int(ident)
        ogg = await self._voice_bytes(body.get("voice_path"))
        try:
            if kind == "bot":
                await self.channel.reply(chat_id, text, ogg)
            else:
                ok, why = self.user.state()
                if not ok:
                    raise ToolError(why, code="unavailable")
                await self.user.reply(chat_id, text, ogg)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError("не удалось доставить в Telegram: " + scrub(str(e), self.channel._token)[:160],
                            code="unavailable") from e
        return {"delivered": True}
