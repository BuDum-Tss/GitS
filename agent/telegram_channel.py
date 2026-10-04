"""Telegram: ассистент периодически публикует «мысли» в канал.

Секреты НЕ в репозитории: токен бота и канал берутся из agent/secrets.env
(файл в .gitignore):
    TELEGRAM_BOT_TOKEN=123456:ABC...        (выдаёт @BotFather)
    TELEGRAM_CHANNEL=@имя_канала            (или числовой id вида -100...)
Бот должен быть администратором канала с правом публиковать сообщения.

Настройки (меняются в трее/дашборде/чате):
    telegram          - публиковать ли вообще (по умолчанию выключено);
    telegram_confirm  - спрашивать ли разрешение вслух перед каждым постом
                        (по умолчанию нет: публикует сама).

Приватность: «мысль» строится только из безопасного материала (время суток, события
игры, её прошлые посты). Экран, услышанное, разговор и данные о пользователе в
запрос не попадают, а готовый текст проверяется фильтром (ссылки, @упоминания,
телефоны, имя пользователя, пароли) и отбрасывается, если что-то нашлось.
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

import approvals
import events_log
import fast_brain
import settings
from tg_draft import TgDraft

logger = logging.getLogger("telegram")

API = "https://api.telegram.org"
EVERY_MINS = int(os.getenv("TELEGRAM_EVERY_MINS", "180"))   # как часто она задумывается о посте
HOURS = (9, 23)                                              # ночью в канал не пишет
MAX_CHARS = 700

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
    def __init__(self, memory_dir: str, say, facts_provider):
        self._file = Path(memory_dir) / "telegram.json"
        self.draft = TgDraft(memory_dir)
        self._say = say                      # как озвучить вопрос о разрешении
        self._facts = facts_provider         # для фильтра: имя пользователя не должно попасть в пост
        self._token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self._channel = os.getenv("TELEGRAM_CHANNEL", "").strip()
        self._verified = False
        self.title = None
        self._warned = False
        self._last_consult = 0.0
        self.rel = None          # Relationships: профили людей, написавших боту
        self.on_contact = None   # (имя, текст, новый_ли) -> None: запись в память
        self._inbox_warned = False

    # ── состояние ────────────────────────────────────────────────────────────

    def configured(self) -> bool:
        return bool(self._token and self._channel)

    def _load(self) -> dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            return {"last_post": 0.0, "posts": []}

    def _save(self, data: dict):
        data["posts"] = data["posts"][-30:]
        self._file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Bot API ───────────────────────────────────────────────────────────────

    async def _call(self, method: str, _timeout: float = 15, **params) -> dict:
        async with httpx.AsyncClient(timeout=_timeout) as c:
            r = await c.post(f"{API}/bot{self._token}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "ошибка Telegram"))
        return data["result"]

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
            events_log.log("telegram", f"проверка не пройдена: {e}")
            logger.warning(f"Telegram: {e}")
            return False
        self.title = chat.get("title")
        events_log.log("telegram", f"бот @{me.get('username')} подключён к каналу «{chat.get('title')}»")
        return True

    async def _upload(self, method: str, data: dict, files: dict) -> dict:
        async with httpx.AsyncClient(timeout=90) as c:
            r = await c.post(f"{API}/bot{self._token}/{method}", data=data, files=files)
        res = r.json()
        if not res.get("ok"):
            raise RuntimeError(res.get("description", "ошибка Telegram"))
        return res["result"]

    async def send_photos(self, paths: list[Path], caption: str = "") -> bool:
        """Фото в канал: одно через sendPhoto, 2-4 альбомом. Подпись до 1024 символов; длиннее уйдёт отдельным сообщением."""
        import json as _json
        try:
            cap = caption if len(caption) <= 1024 else ""
            if len(paths) == 1:
                p = paths[0]
                await self._upload("sendPhoto", {"chat_id": self._channel, "caption": cap},
                                   {"photo": (p.name, p.read_bytes())})
            else:
                media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": cap} if i == 0 and cap else {})}
                         for i in range(len(paths))]
                await self._upload("sendMediaGroup", {"chat_id": self._channel, "media": _json.dumps(media)},
                                   {f"p{i}": (p.name, p.read_bytes()) for i, p in enumerate(paths)})
            if caption and not cap:
                await self._call("sendMessage", chat_id=self._channel, text=caption)
            return True
        except Exception as e:
            events_log.log("error", f"telegram: не удалось отправить фото: {e}")
            return False

    async def send_voice(self, text: str) -> bool:
        """Голосовое сообщение в канал: текст озвучивается (Fish Speech или запасной голос), OGG/Opus, текст подписью."""
        import opus_encoder
        import tts_engine
        path = None
        try:
            path = await asyncio.to_thread(tts_engine.synth_file, text)
            if not path:
                raise RuntimeError("синтез речи не дал аудио")
            ogg = await opus_encoder.audio_file_to_ogg_opus(path)
            cap = text if len(text) <= 1024 else ""
            await self._upload("sendVoice", {"chat_id": self._channel, "caption": cap}, {"voice": ("voice.ogg", ogg)})
            if text and not cap:
                await self._call("sendMessage", chat_id=self._channel, text=text)
            return True
        except Exception as e:
            events_log.log("error", f"telegram: голосовое не отправилось, пойдёт текстом: {e}")
            return False
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def send_post(self, text: str, voice: bool | None = None) -> bool:
        """Опубликовать текст: голосом (если voice или включено в настройках), при сбое голоса обычным текстом."""
        want = settings.load().get("telegram_voice", False) if voice is None else voice
        if want and await self.send_voice(text):
            return True
        return await self.send(text)

    async def compose(self, instruction: str, current: str = "") -> str | None:
        """Написать или переписать текст поста по задаче; при непустом current правит его.
        None, если не получилось или текст не прошёл фильтр приватности."""
        game = events_log.tail(limit=8, kinds={"game"}, since=time.time() - 6 * 3600)
        material = ("\nСобытия игры за последние часы (колония, безопасно упоминать):\n"
                    + "\n".join(f"  - {e['text'][:140]}" for e in game)) if game else ""
        past = [p["text"][:140] for p in self._load()["posts"][-3:]]
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
            events_log.log("error", f"telegram: не удалось написать черновик: {e}")
            return None
        return self._safe(str(res.get("text", ""))) if res else None

    async def publish(self, text: str, photos: list[Path], voice: bool | None = None) -> tuple[str, str]:
        """Опубликовать готовое: текст и/или снимки. Статусы как у post_on_request, плюс empty."""
        if not self.configured():
            return "not_configured", ""
        if not self._verified:
            self._verified = await self.verify()
            if not self._verified:
                return "not_verified", ""
        text = text.strip()
        if text:
            text = self._safe(text)
            if not text:
                events_log.log("telegram", "черновик отброшен фильтром приватности")
                return "unsafe", ""
        if not text and not photos:
            return "empty", ""
        if settings.load()["telegram_confirm"]:
            what = f"«{text}»" + (f" и {len(photos)} скриншот(а)" if photos else "")
            if not await approvals.request(f"Хочу опубликовать в канал: {what}.", self._say):
                events_log.log("telegram", "публикация отклонена пользователем")
                return "declined", text
        want_voice = settings.load().get("telegram_voice", False) if voice is None else voice
        if photos:
            ok = await self.send_photos(photos, "" if (want_voice and text) else text)
            if ok and want_voice and text:
                await self.send_post(text, True)
        else:
            ok = await self.send_post(text, want_voice)
        if not ok:
            return "failed", text
        data = self._load()
        data["last_post"] = time.time()
        data["posts"].append({"ts": data["last_post"], "text": text or "[скриншоты]"})
        self._save(data)
        events_log.log("telegram", f"опубликовано из черновика ({len(photos)} фото): " + (text or "[скриншоты]"))
        return "ok", text

    async def send(self, text: str) -> bool:
        try:
            await self._call("sendMessage", chat_id=self._channel, text=text)
            return True
        except Exception as e:
            events_log.log("error", f"telegram: не удалось опубликовать: {e}")
            return False

    def status_text(self) -> str:
        """Честное описание состояния для самой ассистентки (в её контекст)."""
        if not self.configured():
            return "Telegram-канал: не настроен (в agent/secrets.env нет токена или канала)."
        name = f"«{self.title}»" if self.title else self._channel
        link = "подключён" if self._verified else "настроен, но связь ещё не проверялась"
        periodic = "включены" if settings.load()["telegram"] else "выключены"
        return (f"Telegram-канал {name}: {link}; периодические посты {periodic}. "
                "По просьбе пользователя ты МОЖЕШЬ опубликовать в него пост (отдельное действие, "
                "которое выполняет система, а не ты текстом в чате).")

    async def post_on_request(self, instruction: str, voice: bool | None = None) -> tuple[str, str]:
        """Пост по прямой просьбе пользователя (работает и при выключенных периодических постах).
        Возвращает (статус, текст): ok / not_configured / not_verified / unsafe / declined / failed."""
        if not self.configured():
            return "not_configured", ""
        if not self._verified:
            self._verified = await self.verify()
            if not self._verified:
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
            events_log.log("error", f"telegram: не удалось сочинить пост: {e}")
            return "failed", ""
        text = self._safe(str(res.get("text", ""))) if res else None
        if text is None:
            events_log.log("telegram", "черновик по просьбе отброшен фильтром приватности")
            return "unsafe", ""
        if settings.load()["telegram_confirm"]:
            if not await approvals.request(f"Хочу опубликовать в канал: «{text}».", self._say):
                events_log.log("telegram", "публикация отклонена пользователем")
                return "declined", text
        if not await self.send_post(text, voice):
            return "failed", text
        data = self._load()
        data["last_post"] = time.time()
        data["posts"].append({"ts": data["last_post"], "text": text})
        self._save(data)
        events_log.log("telegram", "опубликовано по просьбе: " + text)
        return "ok", text

    # ── главный цикл ───────────────────────────────────────────────────────────

    # ── входящие: кто-то написал боту ────────────────────────────────────────

    def _on_private(self, msg: dict):
        """Личное сообщение боту: завести профиль человека, запомнить, что он написал. Сама не отвечает."""
        sender = msg.get("from") or {}
        if sender.get("is_bot") or self.rel is None:
            return
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) \
            or sender.get("username") or f"id{sender.get('id')}"
        text = (msg.get("text") or msg.get("caption") or "[не текст]").strip()
        key = f"tg:{sender.get('id')}"
        new = self.rel.ensure_person(key, name, source="telegram", handle=sender.get("username", ""),
                                     tg_id=sender.get("id"))
        self.rel.note(key, text)
        events_log.log("telegram", f"{'новый собеседник' if new else 'сообщение'} в боте: {name}: {text[:120]}")
        if self.on_contact:
            try:
                self.on_contact(name, text, new)
            except Exception as e:
                logger.warning(f"Telegram: запись контакта: {e}")

    async def _inbox_loop(self):
        while True:
            if not self._token:
                await asyncio.sleep(300)
                continue
            try:
                off = self._load().get("update_offset")
                params = {"timeout": 25, "allowed_updates": ["message"]}
                if off:
                    params["offset"] = off
                updates = await self._call("getUpdates", _timeout=40, **params)
                for u in updates:
                    msg = u.get("message")
                    if msg and (msg.get("chat") or {}).get("type") == "private":
                        self._on_private(msg)
                if updates:
                    data = self._load()
                    data["update_offset"] = updates[-1]["update_id"] + 1
                    self._save(data)
                self._inbox_warned = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._inbox_warned:
                    self._inbox_warned = True
                    events_log.log("telegram", f"входящие сообщения бота не читаются: {e}")
                await asyncio.sleep(60)

    async def run(self):
        asyncio.create_task(self._inbox_loop())
        while True:
            await asyncio.sleep(60)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Telegram: {e}")
                events_log.log("error", f"telegram: {e}")

    async def _tick(self):
        cfg = settings.load()
        if not cfg["telegram"]:
            return
        if not self.configured():
            if not self._warned:
                self._warned = True
                events_log.log("telegram", "включено, но не заданы TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL "
                                           "в agent/secrets.env")
            return
        if not self._verified:
            self._verified = await self.verify()
            if not self._verified:
                await asyncio.sleep(600)   # не долбить Telegram при неверных настройках
                return
        now = time.time()
        if not (HOURS[0] <= time.localtime(now).tm_hour < HOURS[1]):
            return
        if settings.quiet_active():
            return   # пользователь просил не отвлекать: вопросов вслух не задаём
        data = self._load()
        if now - data["last_post"] < EVERY_MINS * 60 or now - self._last_consult < EVERY_MINS * 20:
            return
        self._last_consult = now
        text = await self._think(data)
        if not text:
            return
        if cfg["telegram_confirm"]:
            if not await approvals.request(f"Хочу опубликовать в канал: «{text}».", self._say):
                events_log.log("telegram", "публикация отклонена пользователем")
                return
        if await self.send_post(text):
            data["last_post"] = time.time()
            data["posts"].append({"ts": data["last_post"], "text": text})
            self._save(data)
            events_log.log("telegram", "опубликовано: " + text)

    # ── сочинение мысли ─────────────────────────────────────────────────────────

    def _digest(self, data: dict) -> str:
        now = time.time()
        lines = [f"Сейчас {time.strftime('%H:%M')}."]
        game = events_log.tail(limit=8, kinds={"game"}, since=now - 6 * 3600)
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
        """Вернуть текст или None, если в нём есть то, чего в публичном канале быть не должно."""
        text = re.sub(r"[*`]+", "", text).strip()
        if not text or len(text) > MAX_CHARS or _UNSAFE.search(text):
            return None
        low = text.lower()
        for fact in self._facts():
            m = re.match(r"\s*(?:имя(?: пользователя)?|зовут)\s*[:\-]\s*(.+)", fact, re.I)
            if m:
                for name in re.findall(r"[А-Яа-яЁёA-Za-z]{3,}", m.group(1)):
                    if name.lower()[:4] in low:
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
                    self._digest(data), timeout=90, background=True, max_tokens=300, temperature=0.95))
            except Exception as e:
                logger.warning(f"Не удалось сочинить пост: {e}")
                return None
            if not res or not res.get("post") or not res.get("text"):
                events_log.log("telegram", "задумалась о посте и решила промолчать")
                return None
            text = self._safe(str(res["text"]))
            if text is None or self._style_ok(text, data):
                break
            events_log.log("telegram", "черновик отброшен как вялый, пишу заново")
            text = "" if attempt == 1 else None
        if text == "":
            return None
        if text is None:
            events_log.log("telegram", "черновик отброшен фильтром приватности")
        return text
