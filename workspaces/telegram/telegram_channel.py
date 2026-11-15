"""Telegram-канал и бот: она периодически публикует «мысли» в канал, пишет посты по просьбе и отвечает владельцу в боте.

Секреты НЕ в репозитории: токен бота и канал модуль читает сам из .env (файл в .gitignore):
    TELEGRAM_BOT_TOKEN=123456:ABC...        (выдаёт @BotFather)
    TELEGRAM_CHANNEL=@имя_канала            (или числовой id вида -100...)
Бот должен быть администратором канала с правом публиковать сообщения.

Настройки приходят от ядра (ТЗ 10, TG-2): общие в client.settings (quiet_until, dnd), свои в client.module_settings
(settings_schema, хранятся в peers.telegram):
    publish     - публиковать ли «мысли» сама (по умолчанию выключено);
    after_run   - писать ли пост-итог после забега (просит модуль игры, tool telegram.message с after_run);
    voice       - посты голосовым сообщением (текст идёт подписью);
    every_mins, post_hours_from, post_hours_to - частота и окно часов;
    owner, owner_tg_id - владелец (@имя или числовой id).
Разрешения на публикацию в канал нет: она публикует сама (отправка с её личного аккаунта в незнакомые чаты спрашивается).

Приватность: «мысль» строится только из безопасного материала (время суток, события игры, её прошлые посты). Экран,
услышанное, разговор и данные о пользователе в запрос не попадают, а готовый текст проверяется фильтром (ссылки,
@упоминания, телефоны, личные слова от ядра, пароли) и отбрасывается, если что-то нашлось.

Личные сообщения боту становятся событиями telegram.bot_message: ядро само решает, что с ними делать. Владельцу отвечает
она сама (разговор, deliver); любому другому человеку отвечает тот же агент тем же разговором (R5, без отличий в контексте и
возможностях), ответ тоже приходит запросом deliver. Сам модуль никому
не отвечает: он только доставляет то, что прислало ядро. Владелец определяется настройками owner_tg_id и owner (@имя или id).
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import httpx
from common import CAPTION_MAX, KnownChats, OwnerBook, chunks, full_name, message_fact, quiet_active, scrub
from shots import Shots

from gits_sdk import host

logger = logging.getLogger("telegram")

API = "https://api.telegram.org"
MAX_CHARS = 700
STATE_FILE = "telegram.json"
_MEDIA = {"photo": ("sendPhoto", "photo"), "video": ("sendVideo", "video"), "voice": ("sendVoice", "voice"),
          "document": ("sendDocument", "document")}

_UNSAFE = re.compile(r"https?://|www\.|@\w|\S+@\S+\.\S+|\+?\d[\d\s\-()]{8,}|парол|password|token|токен", re.I)
_BANNED_OPENERS = ("итак", "наблюдаю", "кажется", "ну вот", "что ж", "ох")
_STOCK = re.compile(r"наблюда|хаос|человеческ|любопытств|заблудш", re.I)



class TelegramChannel:
    def set_persona(self, sections: dict) -> None:
        """Разделы персоны от ядра (core.persona, ТЗ 05 CHR-4): канал пишет от лица персонажа, без имени в коде."""
        self._persona_sections = dict(sections or {})

    def _persona_text(self) -> str:
        return str(getattr(self, "_persona_sections", {}).get("telegram_channel", "")).strip()

    def _formats(self) -> list[str]:
        raw = str(getattr(self, "_persona_sections", {}).get("telegram_formats", ""))
        return [line[2:].strip() for line in raw.splitlines() if line.startswith("- ")]

    def __init__(self, memory_dir: str, client, privacy):
        """client - WorkspaceClient (settings, ask, request, event); privacy - Privacy (личные слова от ядра, TG-5)."""
        self._file = Path(memory_dir) / STATE_FILE
        self.shots = Shots(memory_dir)
        self.owner = OwnerBook(memory_dir, client)
        self.known = KnownChats(memory_dir)
        self.client = client
        self.privacy = privacy
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._channel = os.getenv("TELEGRAM_CHANNEL", "").strip()
        self.verified = False
        self.title = None
        self._warned = False
        self._last_consult = 0.0
        self._inbox_warned = False

    # ── настройки ядра ───────────────────────────────────────────────────────

    @property
    def cfg(self) -> dict:
        return self.client.settings

    def flag(self, name: str) -> bool:
        """Логическая собственная настройка (publish, after_run, voice); нет значения - выключена."""
        return (self.client.module_settings or {}).get(name) is True

    def mod_cfg(self, name: str, default):
        value = (self.client.module_settings or {}).get(name, default)
        return default if isinstance(value, bool) or not isinstance(value, type(default)) else value

    def owner_id(self) -> int:
        """Числовой id владельца (для deliver target=owner): owner_tg_id, число в `owner` или выученный по @имени."""
        return self.owner.owner_id()

    def every_mins(self) -> int:
        """Как часто она задумывается о посте: настройка модуля; TELEGRAM_EVERY_MINS остаётся запасной, пока настройка
        не менялась (равна значению по умолчанию)."""
        value = self.mod_cfg("every_mins", 180)
        env = os.getenv("TELEGRAM_EVERY_MINS", "").strip()
        if value == 180 and env.isdigit() and int(env) > 0:
            return int(env)
        return max(1, value)

    def post_hours(self) -> tuple[int, int]:
        return self.mod_cfg("post_hours_from", 9), self.mod_cfg("post_hours_to", 23)

    # ── состояние ────────────────────────────────────────────────────────────

    def configured(self) -> bool:
        return bool(self._token and self._channel)

    def has_token(self) -> bool:
        return bool(self._token)

    def _load(self) -> dict:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data.setdefault("last_post", 0.0)
        data.setdefault("posts", [])
        return data

    def _save(self, data: dict):
        data["posts"] = data["posts"][-30:]
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def aload(self) -> dict:
        return await asyncio.to_thread(self._load)

    async def asave(self, data: dict) -> None:
        await asyncio.to_thread(self._save, data)

    def last_post_ts(self) -> float:
        return float(self._load().get("last_post") or 0.0)

    def log(self, text: str, error: bool = False, **data) -> None:
        """Запись в общий журнал (вид telegram или error); токен бота из текста вырезается."""
        host.log_nowait("error" if error else "telegram", scrub(("telegram: " if error else "") + text, self._token),
                              **data)

    # ── Bot API ───────────────────────────────────────────────────────────────

    async def _call(self, method: str, _timeout: float = 15, **params) -> dict:
        async with httpx.AsyncClient(timeout=_timeout) as c:
            r = await c.post(f"{API}/bot{self._token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "ошибка Telegram"))
        return data["result"]

    async def _upload(self, method: str, data: dict, files: dict) -> dict:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{API}/bot{self._token}/{method}", data=data, files=files)
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError(res.get("description", "ошибка Telegram"))
        return res["result"]

    async def verify(self) -> bool:
        """Проверка один раз: токен верный, канал виден, бот может публиковать."""
        try:
            me = await self._call("getMe")
            chat = await self._call("getChat", chat_id=self._channel)
            member = await self._call("getChatMember", chat_id=self._channel, user_id=me["id"])
            can_post = member.get("status") == "creator" or member.get("can_post_messages")
            if not can_post:
                raise RuntimeError("бот не администратор канала с правом публиковать сообщения")
        except Exception as e:
            self.log(f"проверка не пройдена: {e}")
            logger.warning("Telegram: %s", scrub(str(e), self._token))
            return False
        self.title = chat.get("title")
        self.log(f"бот @{me.get('username')} подключён к каналу «{chat.get('title')}»")
        return True

    async def ensure_verified(self) -> bool:
        if self.verified:
            return True
        if not self.configured():
            return False
        self.verified = await self.verify()
        return self.verified

    async def send_files(self, chat_id, items: list[tuple[Path, str]], caption: str = "", reply_to: int | None = None) -> None:
        """Файлы в чат от бота (канал или личка). items - (путь, вид: photo | video | voice | document). Один файл: text подписью
        (до 1024 символов; длиннее уйдёт отдельным сообщением). Несколько фото (2-10) идут альбомом с подписью на первом;
        остальное по одному, текст после них отдельным сообщением. Бросает RuntimeError."""
        extra = {"reply_to_message_id": reply_to} if reply_to else {}
        cap = caption if len(caption) <= CAPTION_MAX else ""
        blobs = [await asyncio.to_thread(path.read_bytes) for path, _ in items]
        album = 1 < len(items) <= 10 and all(kind == "photo" for _, kind in items)
        in_caption = False
        if album:
            media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 and cap else {})}
                     for i in range(len(items))]
            await self._upload("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media), **extra},
                               {f"p{i}": (items[i][0].name, blobs[i]) for i in range(len(items))})
            in_caption = bool(cap)
        else:
            for i, ((path, kind), blob) in enumerate(zip(items, blobs, strict=True)):
                method, field = _MEDIA[kind]
                data = {"chat_id": chat_id, **(extra if i == 0 else {})}
                if len(items) == 1 and cap:
                    data["caption"], in_caption = cap, True
                if kind == "video":
                    data["supports_streaming"] = "true"
                await self._upload(method, data, {field: (path.name, blob)})
        if caption and not in_caption:
            await self._call("sendMessage", chat_id=chat_id, text=caption)

    async def synth_ogg(self, text: str) -> bytes:
        """TG-4: озвучка её голосом запросом core.tts (ядро возвращает путь к OGG/Opus); файл читается и удаляется."""
        path = (await self.client.tts(text, None, "ogg", timeout=90)).path
        if not path:
            raise RuntimeError("синтез речи не дал аудио")
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        finally:
            await asyncio.to_thread(_unlink, path)

    async def try_ogg(self, text: str) -> bytes | None:
        """Голос для текста или None (сбой синтеза: сообщение пойдёт текстом)."""
        try:
            return await self.synth_ogg(text)
        except Exception as e:
            self.log(f"голосовое не получилось, пойдёт текстом: {e}", error=True)
            return None

    async def send_post(self, text: str, voice: bool | None = None) -> bool:
        """Опубликовать текст в канал: голосом (если voice или включено в настройках), при сбое голоса обычным текстом."""
        want = self.flag("voice") if voice is None else voice
        ogg = await self.try_ogg(text) if want else None
        try:
            await self.reply(self._channel, text, ogg)
            return True
        except Exception as e:
            self.log(f"не удалось опубликовать: {e}", error=True)
            return False

    # ── ответы в личные чаты бота (deliver) ──────────────────────────────────

    async def reply(self, chat_id: int | str, text: str, ogg: bytes | None = None, reply_to: int | None = None) -> None:
        """Отправить текст от бота в чат (владельцу, в канал или гостю); с ogg - голосовым сообщением с текстом подписью.
        Бросает RuntimeError."""
        if not self._token:
            raise RuntimeError("бот не настроен: нет TELEGRAM_BOT_TOKEN в .env")
        rest = text
        extra = {"reply_to_message_id": reply_to} if reply_to else {}
        if ogg is not None:
            cap = text if len(text) <= CAPTION_MAX else ""
            try:
                await self._upload("sendVoice", {"chat_id": chat_id, "caption": cap, **extra}, {"voice": ("voice.ogg", ogg)})
                rest, extra = ("" if cap else text), {}
            except Exception as e:
                self.log(f"голосовой ответ не ушёл, отправляю текстом: {e}", error=True)
        for part in chunks(rest):
            await self._call("sendMessage", chat_id=chat_id, text=part, **extra)
            extra = {}

    # ── публикация ───────────────────────────────────────────────────────────

    async def _record_post(self, text: str, how: str) -> None:
        data = await self.aload()
        data["last_post"] = time.time()
        data["posts"].append({"ts": data["last_post"], "text": text or "[файлы]"})
        await self.asave(data)
        self.log(f"{how}: " + (text or "[файлы]"))
        await self.client.event("telegram.posted", f"Опубликован пост в канале: «{(text or '[файлы]')[:150]}»")

    async def ready_for_posts(self) -> str | None:
        """None, если в канал можно публиковать; иначе статус (not_configured, unavailable, not_verified)."""
        if not self.configured():
            return "not_configured"
        if not self.privacy.ready:
            return "unavailable"
        if not await self.ensure_verified():
            return "not_verified"
        return None

    async def compose_post(self, instruction: str) -> str | None:
        """Текст по просьбе (тема `topic`) из безопасного материала. None, если не получилось или фильтр приватности
        отбросил."""
        system = (self._persona_text() + f" Владелец просит тебя написать сообщение для канала «{self.title or ''}». Выполни его "
                  "просьбу: если тема не названа, напиши короткий приветственный пост в своём характере. "
                  "Пиши 1-4 предложения. НЕЛЬЗЯ: имена людей, что делает или говорит пользователь, что у "
                  "него на экране, личные данные, ссылки, @упоминания; и ничего не выдумывай о канале "
                  "(его темы, игры, проекты), если об этом не сказано. Ответь ТОЛЬКО JSON: "
                  '{"text": "<текст>"}')
        try:
            res = await host.llm_json(system, f"Просьба владельца: {instruction}", timeout=90, max_tokens=300,
                                      temperature=0.8)
        except Exception as e:
            self.log(f"не удалось сочинить текст: {e}", error=True)
            return None
        text = self._safe(str(res.get("text", ""))) if res else None
        if text is None and res:
            self.log("черновик по просьбе отброшен фильтром приватности")
        return text

    # ── входящие: кто-то написал боту ────────────────────────────────────────

    async def _on_private(self, msg: dict) -> bool:
        """Личное сообщение боту: событие telegram.bot_message (TG-7: профиль человека и память ведёт ядро по sender).
        True, если сообщение обработано (событие ушло или его не нужно слать); False - повторить позже."""
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return True
        chat_id = (msg.get("chat") or {}).get("id")
        uid = sender.get("id")
        if uid is None or chat_id is None:
            return True
        name = full_name(sender.get("first_name"), sender.get("last_name"), sender.get("username"), uid)
        text = (msg.get("text") or msg.get("caption") or "[не текст]").strip()
        await asyncio.to_thread(self.known.add, chat_id, name, sender.get("username"), sender.get("first_name"))
        self.log(f"сообщение в боте: {name}: {text[:120]}")
        try:
            return bool(await self.client.event(
                "telegram.bot_message", message_fact(name, "личке бота", text),
                data={"chat_id": chat_id, "message_id": msg.get("message_id")},
                addressed_to_me=True, sender=self.owner.sender(uid, name, sender.get("username")),
                reply_channel={"module": "telegram", "target": f"bot:{chat_id}"}, memorable=True,
                dedupe_key=f"tg:bot:{chat_id}:{msg.get('message_id')}"))
        except ValueError as e:
            logger.warning("событие бота не сформировано: %s", e)
            return True                  # сообщение неисправимое: пропустить, а не застрять на нём

    async def run_inbox(self):
        """Входящие бота (getUpdates). Пока ядро не принимает события (нет связи, модуль выключен), смещение не двигается:
        сообщения не теряются, а придут, когда связь появится."""
        while True:
            if not self._token:
                await asyncio.sleep(300)
                continue
            if not (self.client.enabled and self.client.connected):
                await asyncio.sleep(3)
                continue
            try:
                off = (await self.aload()).get("update_offset")
                params = {"timeout": 25, "allowed_updates": ["message"]}
                if off:
                    params["offset"] = off
                updates = await self._call("getUpdates", _timeout=40, **params)
                handled, stuck = None, False
                for u in updates:
                    msg = u.get("message")
                    if msg and (msg.get("chat") or {}).get("type") == "private":
                        if not await self._on_private(msg):
                            stuck = True
                            break
                    handled = u["update_id"]
                if handled is not None:
                    data = await self.aload()
                    data["update_offset"] = handled + 1
                    await self.asave(data)
                self._inbox_warned = False
                if stuck:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._inbox_warned:
                    self._inbox_warned = True
                    self.log(f"входящие сообщения бота не читаются: {e}")
                await asyncio.sleep(60)

    # ── главный цикл ───────────────────────────────────────────────────────────

    async def run_verify(self):
        """Проверка канала при старте и повтор, пока она не пройдена (для честного состояния в дашборде)."""
        while True:
            if self.configured() and not self.verified:
                await self.ensure_verified()
                await asyncio.sleep(600 if not self.verified else 60)
            else:
                await asyncio.sleep(60)

    async def run(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Telegram: %s", scrub(str(e), self._token))
                self.log(f"сбой цикла постов: {e}", error=True)

    async def _tick(self):
        cfg = self.cfg
        if not self.flag("publish") or not self.client.enabled or not self.client.connected:
            return
        if not self.configured():
            if not self._warned:
                self._warned = True
                self.log("включено, но не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL в .env")
            return
        if not await self.ensure_verified():
            await asyncio.sleep(600)   # не долбить Telegram при неверных настройках
            return
        now = time.time()
        lo, hi = self.post_hours()
        if not (lo <= time.localtime(now).tm_hour < hi):
            return
        if quiet_active(cfg):
            return   # пользователь просил не отвлекать: вопросов не задаём
        if not self.privacy.ready:
            return
        every = self.every_mins()
        data = await self.aload()
        if now - data["last_post"] < every * 60 or now - self._last_consult < every * 20:
            return
        self._last_consult = now
        text = await self._think(data)
        if not text:
            return
        if await self.send_post(text):
            await self._record_post(text, "опубликовано")

    # ── сочинение мысли ─────────────────────────────────────────────────────────

    async def _digest(self, data: dict) -> str:
        now = time.time()
        lines = [f"Сейчас {time.strftime('%H:%M')}."]
        game = await host.tail(limit=8, kinds={"game"}, since=now - 6 * 3600)
        if game:
            lines.append("События игры за последние часы (колония, это безопасно упоминать):")
            lines += [f"  - {e['text']}" for e in game]
        past = [p["text"] for p in data["posts"][-3:]]
        if past:
            lines.append("Твои прошлые посты (не повторяйся):")
            lines += [f"  - {p[:160]}" for p in past]
        if self._formats():
            lines.append("Формат поста (держись его): " + random.choice(self._formats()) + ".")
        return "\n".join(lines)

    def _style_ok(self, text: str, data: dict) -> bool:
        """Отсев вялых постов: заезженные слова, одинаковое начало с прошлыми, пересказ прошлого."""
        low = text.lower().lstrip(" «\"—-")
        if low.startswith(_BANNED_OPENERS) or len(_STOCK.findall(text)) >= 2:
            return False
        past = [p["text"].lower().lstrip(" «\"—-") for p in data["posts"][-5:]]
        first = low.split()[:2]
        if any(p.split()[:2] == first for p in past):
            return False
        words = set(re.findall(r"\w{5,}", low))
        return not any(words and len(words & set(re.findall(r"\w{5,}", p))) / len(words) > 0.5 for p in past)

    def _safe(self, text: str) -> str | None:
        """Вернуть текст или None, если в нём есть то, чего в публичном канале быть не должно (TG-5)."""
        text = re.sub(r"[*`]+", "", text).strip()
        if not text or len(text) > MAX_CHARS or _UNSAFE.search(text):
            return None
        if self.privacy.leaks(text):
            return None
        return text

    async def _think(self, data: dict) -> str | None:
        if not self._persona_text():       # без персоны от ядра посты не сочиняем (ТЗ 05, CHR-4)
            return None
        system = (self._persona_text() + " Сама реши, есть ли сейчас что сказать публике; если нет, post=false. "
                  "Пиши 2-4 предложения. НЕЛЬЗЯ: имена людей, что делает или говорит пользователь, "
                  "что у него на экране, личные данные, ссылки, @упоминания. МОЖНО: события игры, "
                  "вымышленные истории и отчёты Aperture Science, мелочи быта вообще. Ответь ТОЛЬКО JSON: "
                  '{"post": <true или false>, "text": "<текст поста>"}')
        res = text = None
        for attempt in range(2):
            try:
                res = await host.llm_json(
                    system + ("" if attempt == 0 else " Прошлый черновик был вялым или похожим на прежние: "
                              "напиши совсем иначе, с конкретикой."),
                    await self._digest(data), timeout=90, background=True, max_tokens=300, temperature=0.95)
            except Exception as e:
                logger.warning("Не удалось сочинить пост: %s", e)
                return None
            if not res or not res.get("post") or not res.get("text"):
                self.log("задумалась о посте и решила промолчать")
                return None
            text = self._safe(str(res["text"]))
            if text is None or self._style_ok(text, data):
                break
            self.log("черновик отброшен как вялый, пишу заново")
            text = "" if attempt == 1 else None
        if text == "":
            return None
        if text is None:
            self.log("черновик отброшен фильтром приватности")
        return text


def _unlink(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
