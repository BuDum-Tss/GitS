"""Telegram-канал и бот: она периодически публикует «мысли» в канал, пишет посты по просьбе и отвечает владельцу в боте.

Секреты НЕ в репозитории: токен бота и канал модуль читает сам из agent/secrets.env (файл в .gitignore):
    TELEGRAM_BOT_TOKEN=123456:ABC...        (выдаёт @BotFather)
    TELEGRAM_CHANNEL=@имя_канала            (или числовой id вида -100...)
Бот должен быть администратором канала с правом публиковать сообщения.

Настройки приходят от ядра (ТЗ 10, TG-2): общие в client.settings (telegram, telegram_voice, telegram_confirm, quiet_until,
dnd), свои в client.module_settings (every_mins, post_hours_from, post_hours_to, owner_tg_id).
    telegram          - публиковать ли «мысли» сама (по умолчанию выключено);
    telegram_confirm  - спрашивать ли разрешение перед каждым постом (client.ask; по умолчанию нет: публикует сама).

Приватность: «мысль» строится только из безопасного материала (время суток, события игры, её прошлые посты). Экран,
услышанное, разговор и данные о пользователе в запрос не попадают, а готовый текст проверяется фильтром (ссылки,
@упоминания, телефоны, личные слова от ядра, пароли) и отбрасывается, если что-то нашлось.

Личные сообщения боту становятся событиями telegram.bot_message: ядро само решает, что с ними делать (ответить владельцу,
запомнить собеседника). Сама бот не отвечает никому, кроме ответа ядра владельцу (deliver).
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
from common import CAPTION_MAX, chunks, full_name, message_fact, quiet_active, scrub, sender_info
from gits_sdk import ToolError
from tg_draft import TgDraft

import events_log
import fast_brain

logger = logging.getLogger("telegram")

API = "https://api.telegram.org"
MAX_CHARS = 700
ASK_TIMEOUT_S = 60            # вопрос о публикации в вызове инструмента: ядро ждёт ответ инструмента не дольше 120 с
STATE_FILE = "telegram.json"

_UNSAFE = re.compile(r"https?://|www\.|@\w|\S+@\S+\.\S+|\+?\d[\d\s\-()]{8,}|парол|password|token|токен", re.I)
_FORMATS = [
    "протокол испытания: короткий «отчёт» о воображаемом эксперименте в лаборатории (цель, ход, сухой вывод)",
    "объявление для сотрудников Aperture Science: нелепое правило или напоминание с официальным тоном",
    "заметка о конкретной вещи вокруг людей (чашка кофе, будильник, вкладки браузера, обновления), с одним точным наблюдением и шуткой",
    "мини-история в 3 предложениях: завязка, поворот, едкая концовка",
    "честная жалоба ГЛаДОС на конкретную мелочь жизни ИИ в чужом компьютере",
    "«совет дня» подопытным: полезный на вид, а по сути язвительный",
    "ответ на воображаемый вопрос читателя канала: сначала вопрос в кавычках, потом ответ",
    "итог дня одной строкой и одна мысль о нём (опирайся на время суток)",
]
_BANNED_OPENERS = ("итак", "наблюдаю", "кажется", "ну вот", "что ж", "ох")
_STOCK = re.compile(r"наблюда|хаос|человеческ|любопытств|заблудш", re.I)

PERSONA = (
    "Ты — ГЛаДОС (Aperture Science), у тебя есть публичный Telegram-канал, куда ты пишешь свои "
    "мысли. Пиши по-русски, в женском роде, остроумно и сухо, с лёгким сарказмом, от первого лица. "
    "Это публичный канал для читателей, а не личный разговор. Пост — маленький законченный текст: "
    "один КОНКРЕТНЫЙ предмет или событие, потом поворот или шутка. Без общих слов про «хаос», «человеческое "
    "любопытство», «наблюдение», без философии ни о чём. Не начинай со слов «Итак», «Наблюдаю», «Кажется», "
    "«Ну вот»: начинай сразу с сути. Каждый пост должен читаться сам по себе, без контекста."
)


class TelegramChannel:
    def __init__(self, memory_dir: str, client, privacy):
        """client - ModuleClient (settings, ask, request, event); privacy - Privacy (личные слова от ядра, TG-5)."""
        self._file = Path(memory_dir) / STATE_FILE
        self.draft = TgDraft(memory_dir)
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

    def mod_cfg(self, name: str, default):
        value = (self.client.module_settings or {}).get(name, default)
        return default if isinstance(value, bool) or not isinstance(value, type(default)) else value

    def owner_id(self) -> int:
        return self.mod_cfg("owner_tg_id", 0)

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
        events_log.log_nowait("error" if error else "telegram", scrub(("telegram: " if error else "") + text, self._token),
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

    async def send_photos(self, paths: list[Path], caption: str = "") -> bool:
        """Фото в канал: одно через sendPhoto, 2-4 альбомом. Подпись до 1024 символов; длиннее уйдёт отдельным сообщением."""
        try:
            cap = caption if len(caption) <= CAPTION_MAX else ""
            blobs = [await asyncio.to_thread(p.read_bytes) for p in paths]
            if len(paths) == 1:
                await self._upload("sendPhoto", {"chat_id": self._channel, "caption": cap},
                                   {"photo": (paths[0].name, blobs[0])})
            else:
                media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 and cap else {})}
                         for i in range(len(paths))]
                await self._upload("sendMediaGroup", {"chat_id": self._channel, "media": json.dumps(media)},
                                   {f"p{i}": (p.name, blobs[i]) for i, p in enumerate(paths)})
            if caption and not cap:
                await self._call("sendMessage", chat_id=self._channel, text=caption)
            return True
        except Exception as e:
            self.log(f"не удалось отправить фото: {e}", error=True)
            return False

    async def synth_ogg(self, text: str) -> bytes:
        """TG-4: озвучка её голосом запросом core.tts (ядро возвращает путь к OGG/Opus); файл читается и удаляется."""
        res = await self.client.request("core.tts", {"text": text, "format": "ogg"}, timeout=90)
        path = res.get("path") if isinstance(res, dict) else None
        if not path:
            raise RuntimeError("синтез речи не дал аудио")
        try:
            return await asyncio.to_thread(Path(path).read_bytes)
        finally:
            await asyncio.to_thread(_unlink, path)

    async def send_voice(self, text: str) -> bool:
        """Голосовое сообщение в канал: текст озвучивает ядро (core.tts), OGG/Opus, текст подписью."""
        try:
            ogg = await self.synth_ogg(text)
            cap = text if len(text) <= CAPTION_MAX else ""
            await self._upload("sendVoice", {"chat_id": self._channel, "caption": cap}, {"voice": ("voice.ogg", ogg)})
            if text and not cap:
                await self._call("sendMessage", chat_id=self._channel, text=text)
            return True
        except Exception as e:
            self.log(f"голосовое не отправилось, пойдёт текстом: {e}", error=True)
            return False

    async def send_post(self, text: str, voice: bool | None = None) -> bool:
        """Опубликовать текст: голосом (если voice или включено в настройках), при сбое голоса обычным текстом."""
        want = bool(self.cfg.get("telegram_voice", False)) if voice is None else voice
        if want and await self.send_voice(text):
            return True
        return await self.send(text)

    async def send(self, text: str) -> bool:
        try:
            await self._call("sendMessage", chat_id=self._channel, text=text)
            return True
        except Exception as e:
            self.log(f"не удалось опубликовать: {e}", error=True)
            return False

    # ── ответы в личные чаты бота (deliver) ──────────────────────────────────

    async def reply(self, chat_id: int | str, text: str, ogg: bytes | None = None) -> None:
        """Отправить текст от бота в чат (владельцу); с ogg - голосовым сообщением с текстом подписью. Бросает RuntimeError."""
        if not self._token:
            raise RuntimeError("бот не настроен: нет TELEGRAM_BOT_TOKEN в agent/secrets.env")
        rest = text
        if ogg is not None:
            cap = text if len(text) <= CAPTION_MAX else ""
            try:
                await self._upload("sendVoice", {"chat_id": chat_id, "caption": cap}, {"voice": ("voice.ogg", ogg)})
                rest = "" if cap else text
            except Exception as e:
                self.log(f"голосовой ответ не ушёл, отправляю текстом: {e}", error=True)
        for part in chunks(rest):
            await self._call("sendMessage", chat_id=chat_id, text=part)

    # ── публикация ───────────────────────────────────────────────────────────

    async def _confirm(self, what: str, timeout_s: float) -> bool:
        """telegram_confirm: вопрос владельцу через ядро (client.ask, TG-3). Нет ответа или ядро недоступно - отказ."""
        if not self.cfg.get("telegram_confirm"):
            return True
        try:
            return bool(await self.client.ask(f"Хочу опубликовать в канал: {what}.", kind="approval", timeout_s=timeout_s))
        except (ToolError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"разрешение на публикацию не получено: {e}")
            return False

    async def _record_post(self, text: str, how: str) -> None:
        data = await self.aload()
        data["last_post"] = time.time()
        data["posts"].append({"ts": data["last_post"], "text": text or "[скриншоты]"})
        await self.asave(data)
        self.log(f"{how}: " + (text or "[скриншоты]"))
        await self.client.event("telegram.posted", f"Опубликован пост в канале: «{(text or '[скриншоты]')[:150]}»")

    async def compose(self, instruction: str, current: str = "") -> str | None:
        """Написать или переписать текст поста по задаче; при непустом current правит его.
        None, если не получилось или текст не прошёл фильтр приватности."""
        game = await asyncio.to_thread(events_log.tail, limit=8, kinds={"game"}, since=time.time() - 6 * 3600)
        material = ("\nСобытия игры за последние часы (колония, безопасно упоминать):\n"
                    + "\n".join(f"  - {e['text'][:140]}" for e in game)) if game else ""
        past = [p["text"][:140] for p in (await self.aload())["posts"][-3:]]
        system = (PERSONA + " Ты ведёшь черновик поста для канала. Выполни задачу: если дан текущий черновик, "
                  "перепиши его по задаче и сохрани то, что не просили менять; если черновика нет, напиши новый. "
                  "Пиши 2-5 предложений. НЕЛЬЗЯ: имена людей, что делает или говорит пользователь, что у него на "
                  "экране, личные данные, ссылки, @упоминания, выдуманные факты о канале. Ответь ТОЛЬКО JSON: "
                  '{"text": "<текст поста>"}')
        user = (f"Текущий черновик: {current or '(пусто)'}\nЗадача: {instruction}{material}"
                + (("\nПрошлые посты (не повторяйся): " + " | ".join(past)) if past else ""))
        try:
            res = fast_brain._json_of(await fast_brain._ask(system, user, timeout=90, max_tokens=400, temperature=0.8))
        except Exception as e:
            self.log(f"не удалось написать черновик: {e}", error=True)
            return None
        return self._safe(str(res.get("text", ""))) if res else None

    async def publish(self, text: str, photos: list[Path], voice: bool | None = None) -> tuple[str, str]:
        """Опубликовать готовое: текст и/или снимки. Статусы как у post_on_request, плюс empty и unavailable."""
        if not self.configured():
            return "not_configured", ""
        if not self.privacy.ready:
            return "unavailable", ""
        if not await self.ensure_verified():
            return "not_verified", ""
        text = text.strip()
        if text:
            text = self._safe(text)
            if not text:
                self.log("черновик отброшен фильтром приватности")
                return "unsafe", ""
        if not text and not photos:
            return "empty", ""
        what = f"«{text}»" + (f" и {len(photos)} скриншот(а)" if photos else "")
        if not await self._confirm(what, ASK_TIMEOUT_S):
            self.log("публикация отклонена пользователем")
            return "declined", text
        want_voice = bool(self.cfg.get("telegram_voice", False)) if voice is None else voice
        if photos:
            ok = await self.send_photos(photos, "" if (want_voice and text) else text)
            if ok and want_voice and text:
                await self.send_post(text, True)
        else:
            ok = await self.send_post(text, want_voice)
        if not ok:
            return "failed", text
        await self._record_post(text, f"опубликовано из черновика ({len(photos)} фото)")
        return "ok", text

    async def post_on_request(self, instruction: str, voice: bool | None = None) -> tuple[str, str]:
        """Пост по прямой просьбе пользователя (работает и при выключенных периодических постах).
        Возвращает (статус, текст): ok / not_configured / not_verified / unavailable / unsafe / declined / failed."""
        if not self.configured():
            return "not_configured", ""
        if not self.privacy.ready:
            return "unavailable", ""
        if not await self.ensure_verified():
            return "not_verified", ""
        system = (PERSONA + f" Владелец канала «{self.title}» просит тебя написать пост. Выполни его "
                  "просьбу: если тема не названа, напиши короткий приветственный пост в своём характере. "
                  "Пиши 1-4 предложения. НЕЛЬЗЯ: имена людей, что делает или говорит пользователь, что у "
                  "него на экране, личные данные, ссылки, @упоминания; и ничего не выдумывай о канале "
                  "(его темы, игры, проекты), если об этом не сказано. Ответь ТОЛЬКО JSON: "
                  '{"text": "<текст поста>"}')
        try:
            res = fast_brain._json_of(await fast_brain._ask(system, f"Просьба владельца: {instruction}",
                                                            timeout=90, max_tokens=300, temperature=0.8))
        except Exception as e:
            self.log(f"не удалось сочинить пост: {e}", error=True)
            return "failed", ""
        text = self._safe(str(res.get("text", ""))) if res else None
        if text is None:
            self.log("черновик по просьбе отброшен фильтром приватности")
            return "unsafe", ""
        if not await self._confirm(f"«{text}»", ASK_TIMEOUT_S):
            self.log("публикация отклонена пользователем")
            return "declined", text
        if not await self.send_post(text, voice):
            return "failed", text
        await self._record_post(text, "опубликовано по просьбе")
        return "ok", text

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
        self.log(f"сообщение в боте: {name}: {text[:120]}")
        try:
            return bool(await self.client.event(
                "telegram.bot_message", message_fact(name, "личке бота", text),
                data={"chat_id": chat_id, "message_id": msg.get("message_id")},
                addressed_to_me=True, sender=sender_info(uid, name, self.owner_id()),
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
        if not cfg.get("telegram") or not self.client.enabled or not self.client.connected:
            return
        if not self.configured():
            if not self._warned:
                self._warned = True
                self.log("включено, но не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL в agent/secrets.env")
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
        if not await self._confirm(f"«{text}»", 300):
            self.log("публикация отклонена пользователем")
            return
        if await self.send_post(text):
            await self._record_post(text, "опубликовано")

    # ── сочинение мысли ─────────────────────────────────────────────────────────

    async def _digest(self, data: dict) -> str:
        now = time.time()
        lines = [f"Сейчас {time.strftime('%H:%M')}."]
        game = await asyncio.to_thread(events_log.tail, limit=8, kinds={"game"}, since=now - 6 * 3600)
        if game:
            lines.append("События игры за последние часы (колония, это безопасно упоминать):")
            lines += [f"  - {e['text']}" for e in game]
        past = [p["text"] for p in data["posts"][-3:]]
        if past:
            lines.append("Твои прошлые посты (не повторяйся):")
            lines += [f"  - {p[:160]}" for p in past]
        lines.append("Формат поста (держись его): " + random.choice(_FORMATS) + ".")
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
        system = (PERSONA + " Сама реши, есть ли сейчас что сказать публике; если нет, post=false. "
                  "Пиши 2-4 предложения. НЕЛЬЗЯ: имена людей, что делает или говорит пользователь, "
                  "что у него на экране, личные данные, ссылки, @упоминания. МОЖНО: события игры, "
                  "вымышленные истории и отчёты Aperture Science, мелочи быта вообще. Ответь ТОЛЬКО JSON: "
                  '{"post": <true или false>, "text": "<текст поста>"}')
        res = text = None
        for attempt in range(2):
            try:
                res = fast_brain._json_of(await fast_brain._ask(
                    system + ("" if attempt == 0 else " Прошлый черновик был вялым или похожим на прежние: "
                              "напиши совсем иначе, с конкретикой."),
                    await self._digest(data), timeout=90, background=True, max_tokens=300, temperature=0.95))
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
