"""Общее для модуля Telegram: имена, отправитель события, факт-текст события, режим «не отвлекать», пути.

Модуль не читает настройки ядра сам (`settings` ядра нельзя импортировать): общие настройки приходят от ядра в
`client.settings`, свои в `client.module_settings` (ТЗ 10, TG-2).
"""
import json
import os
from gits_sdk import resources
import re
import threading
import time
from pathlib import Path
from gits_sdk.envcompat import env

TEXT_MAX = 300                    # TG-10: сколько символов сообщения идёт в факт события
TG_MESSAGE_MAX = 4096             # предел длины одного сообщения Telegram
CAPTION_MAX = 1024                # предел подписи к фото и голосовому
SERVICE_USER_ID = 777000          # служебный аккаунт Telegram: коды входа и уведомления, в события не идут

GITS_ROOT = Path(__file__).resolve().parent.parent.parent


def default_memory_dir() -> str:
    """Папка памяти ядра: MEMORY_DIR (её передаёт супервизор), иначе папка памяти персонажа (resources/<Имя>/memory)."""
    return os.getenv("MEMORY_DIR") or str(resources.default_memory_dir())


def secrets_file() -> Path:
    """Файл секретов: .env (GITS_SECRETS_FILE переопределяет путь, например в проверках запуска)."""
    return Path(env("GITS_SECRETS_FILE") or GITS_ROOT / ".env")


def quiet_active(cfg: dict) -> bool:
    """Идёт ли режим «не отвлекать»: включён вручную (dnd) или по таймеру (quiet_until)."""
    return bool(cfg.get("dnd")) or time.time() < float(cfg.get("quiet_until") or 0.0)


def full_name(first: str | None, last: str | None, username: str | None, ident) -> str:
    name = " ".join(x for x in (first, last) if x)
    return name or username or f"id{ident}"


def _handle(value) -> str:
    """@имя без @ и без регистра; пусто, если не задано."""
    return str(value or "").strip().lstrip("@").strip().lower()


def owner_settings(module_settings: dict | None) -> tuple[int, str]:
    """(owner_tg_id, owner) из собственных настроек модуля: числовой id владельца и строка «@имя или id» (оба необязательны)."""
    cfg = module_settings or {}
    raw_id = cfg.get("owner_tg_id", 0)
    owner_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else 0
    owner = cfg.get("owner")
    return owner_id, owner.strip() if isinstance(owner, str) else ""


def matches_owner(ident, username, owner_id: int = 0, owner: str = "") -> bool:
    """Это владелец: числовой id == owner_tg_id, либо строка `owner` совпадает с @username отправителя (без @ и без
    регистра), либо равна его числовому id."""
    if owner_id and ident == owner_id:
        return True
    want = _handle(owner)
    if not want:
        return False
    if want.isdigit():
        return str(ident) == want
    return bool(username) and _handle(username) == want


def sender_info(ident, name: str, owner_id: int, owner: str = "", username: str | None = None,
                is_owner: bool | None = None) -> dict:
    """sender события: id вида tg:<id>, имя и признак владельца (TG-9: только при заданном владельце). is_owner можно
    передать готовым (его уже решила `OwnerBook`, с учётом выученного id)."""
    owned = matches_owner(ident, username, owner_id, owner) if is_owner is None else bool(is_owner)
    return {"id": f"tg:{ident}", "name": name, "is_owner": owned}


class OwnerBook:
    """Кто владелец: настройки `owner_tg_id` и `owner` (@имя или id) плюс выученный числовой id (файл tg_owner.json рядом с
    telegram.json). Нужен, чтобы `deliver target=owner` работал по id, когда владелец задан только @именем: id запоминается при
    первом его сообщении. Выученный id действует, только пока строка `owner` в настройках та же, для которой он выучен."""

    FILE = "tg_owner.json"

    def __init__(self, memory_dir: str, client):
        self._file = Path(memory_dir) / self.FILE
        self.client = client

    def _settings(self) -> tuple[int, str]:
        return owner_settings(self.client.module_settings)

    def _learned(self, owner: str) -> int:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            value = data.get("owner_id_learned")
            same = _handle(data.get("learned_for")) == _handle(owner)
        except Exception:
            return 0
        return value if same and isinstance(value, int) and not isinstance(value, bool) else 0

    def owner_id(self) -> int:
        """Числовой id владельца для доставки: owner_tg_id, иначе число в `owner`, иначе выученный; 0 - неизвестен."""
        owner_id, owner = self._settings()
        if owner_id:
            return owner_id
        if _handle(owner).isdigit():
            return int(_handle(owner))
        return self._learned(owner)

    def is_owner(self, ident, username: str | None = None) -> bool:
        """Владелец ли отправитель; нашли по @имени - запоминаем его id (owner_id_learned)."""
        owner_id, owner = self._settings()
        if matches_owner(ident, username, owner_id, owner):
            if _handle(owner) and not _handle(owner).isdigit() and isinstance(ident, int):
                self._learn(ident, owner)
            return True
        return bool(ident) and ident == self._learned(owner)

    def _learn(self, ident: int, owner: str) -> None:
        if self._learned(owner) == ident:
            return
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"owner_id_learned": ident, "learned_for": _handle(owner)}), encoding="utf-8")
        except OSError:
            pass                                    # не записалось: в этот раз владельца всё равно узнали по @имени

    def is_target(self, raw) -> bool:
        """Адресат `to` указывает на владельца: «owner», его числовой id или @имя из настроек."""
        owner_id, owner = self._settings()
        want = _handle(raw)
        if not want:
            return False
        if want in ("owner", "владелец"):
            return True
        if want.isdigit():
            return int(want) in {i for i in (owner_id, self.owner_id()) if i}
        return bool(_handle(owner)) and want == _handle(owner)

    def sender(self, ident, name: str, username: str | None = None) -> dict:
        """sender события с признаком владельца."""
        info = sender_info(ident, name, 0, is_owner=self.is_owner(ident, username))
        if username:
            info["username"] = "@" + _handle(username)                     # как к человеку обратиться в чате
        return info


class KnownChats:
    """Чаты, из которых ей уже писали (входящие бота и её аккаунта): в такие можно отвечать без вопроса. Хранятся только
    ключи для сопоставления (id чата, название, @имя, имя собеседника в нижнем регистре), не тексты; файл tg_known.json."""

    FILE = "tg_known.json"
    MAX = 400

    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / self.FILE

    @staticmethod
    def _key(value) -> str:
        return " ".join(str(value or "").strip().lstrip("@").lower().split())

    def _load(self) -> list[str]:
        try:
            keys = json.loads(self._file.read_text(encoding="utf-8")).get("keys", [])
            return [k for k in keys if isinstance(k, str)]
        except Exception:
            return []

    def add(self, *values) -> None:
        new = [k for k in (self._key(v) for v in values) if k]
        keys = self._load()
        fresh = [k for k in new if k not in keys]
        if not fresh:
            return
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"keys": (keys + fresh)[-self.MAX:]}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def has(self, value) -> bool:
        key = self._key(value)
        return bool(key) and key in self._load()


class Dialogs:
    """Её собственная активность по чатам аккаунта (файл tg_dialogs.json): когда и сколько она писала, когда ей отвечали и
    с кем она сейчас говорит. Нужна для продолжения диалога (повышенное внимание к тому, с кем она только что говорила) и
    чтобы инструмент inbox показывал ей её же активность вместо жёстких лимитов. Хранятся только числа и id, не тексты."""

    FILE = "tg_dialogs.json"
    MAX_CHATS = 200
    KEEP_S = 2 * 86400                   # сколько помнить её сообщения (нужны последний час и сутки)
    SENT_MAX = 300

    def __init__(self, memory_dir: str, clock=time.time):
        self._file = Path(memory_dir) / self.FILE
        self._lock = threading.Lock()
        self._clock = clock

    def _load(self) -> dict:
        try:
            chats = json.loads(self._file.read_text(encoding="utf-8")).get("chats", {})
        except Exception:
            return {}
        return {k: v for k, v in chats.items() if isinstance(k, str) and isinstance(v, dict)}

    def _save(self, chats: dict) -> None:
        if len(chats) > self.MAX_CHATS:                                  # старейшие по последней активности уходят
            last = lambda k: max(chats[k].get("reply_ts", 0), chats[k].get("in_ts", 0))     # noqa: E731
            chats = {k: chats[k] for k in sorted(chats, key=last)[-self.MAX_CHATS:]}
        tmp = self._file.with_name(self._file.name + ".tmp")
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"chats": chats}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            pass

    def note_out(self, chat_id) -> None:
        """Она написала в чат (ответ, сообщение, файл)."""
        if chat_id is None:
            return
        now = self._clock()
        with self._lock:
            chats = self._load()
            rec = chats.setdefault(str(chat_id), {})
            sent = [t for t in rec.get("sent", []) if now - t < self.KEEP_S]
            sent.append(now)
            rec["sent"] = sent[-self.SENT_MAX:]
            rec["reply_ts"] = now
            self._save(chats)

    def note_in(self, chat_id, sender_id=None, addressed: bool = False) -> None:
        """Ей написали в чат; addressed - обращение к ней (в личке всегда): этот человек становится её собеседником."""
        with self._lock:
            chats = self._load()
            rec = chats.setdefault(str(chat_id), {})
            rec["in_ts"] = self._clock()
            if addressed and sender_id is not None:
                rec["peer"] = sender_id
            self._save(chats)

    def in_dialog(self, chat_id, sender_id, window_s: float) -> bool:
        """Она недавно (не позже window_s назад) отвечала в этом чате, а sender_id тот, с кем она говорила."""
        rec = self._load().get(str(chat_id)) or {}
        return bool(window_s > 0 and sender_id is not None and rec.get("peer") == sender_id
                    and self._clock() - float(rec.get("reply_ts") or 0.0) <= window_s)

    def activity(self, chat_id) -> dict:
        """{"hour": сколько она написала за час, "day": за сутки, "last_out": ts или 0, "last_in": ts или 0}."""
        rec = self._load().get(str(chat_id)) or {}
        now = self._clock()
        sent = [t for t in rec.get("sent", []) if isinstance(t, (int, float))]
        return {"hour": sum(1 for t in sent if now - t < 3600), "day": sum(1 for t in sent if now - t < 86400),
                "last_out": float(rec.get("reply_ts") or 0.0), "last_in": float(rec.get("in_ts") or 0.0)}


# Режимы чата её аккаунта (жёстко в коде telegram_user.py): что ей позволено в этом чате
MODE_IGNORE, MODE_READ, MODE_REPLY = "ignore", "read", "reply"
CHAT_MODES = (MODE_IGNORE, MODE_READ, MODE_REPLY)
MODE_WORDS = {MODE_IGNORE: "игнор", MODE_READ: "чтение", MODE_REPLY: "ответы"}


def valid_mode(value, default: str = MODE_REPLY) -> str:
    """Режим из настройки или файла: незнакомое значение заменяется на default."""
    return value if value in CHAT_MODES else default


class ChatModes:
    """Режим каждого чата её аккаунта: ignore (событий в ядро нет), read (события есть, писать нельзя), reply (как обычно).
    Файл tg_chat_modes.json рядом с остальной памятью; хранятся только id чата и режим, не тексты. Запись атомарная (временный
    файл и замена). Для чата без записи действует режим по умолчанию (настройка `default_chat_mode`, по умолчанию reply)."""

    FILE = "tg_chat_modes.json"

    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / self.FILE
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        try:
            chats = json.loads(self._file.read_text(encoding="utf-8")).get("chats", {})
        except Exception:
            return {}
        return {k: v for k, v in chats.items() if isinstance(k, str) and k.lstrip("-").isdigit() and v in CHAT_MODES}

    def _save(self, chats: dict[str, str]) -> bool:
        tmp = self._file.with_name(self._file.name + ".tmp")
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"chats": chats}, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            return False
        return True

    def get(self, chat_id, default: str = MODE_REPLY) -> str:
        return self._load().get(str(chat_id)) or valid_mode(default)

    def explicit(self, chat_id) -> str | None:
        """Режим, который для чата выбрали явно; None, если чат на значении по умолчанию."""
        return self._load().get(str(chat_id))

    def set(self, chat_id, mode: str) -> bool:
        if mode not in CHAT_MODES:
            raise ValueError(f"режим чата: одно из {list(CHAT_MODES)}")
        with self._lock:
            chats = self._load()
            chats[str(chat_id)] = mode
            return self._save(chats)

    def all(self) -> dict[str, str]:
        return self._load()


class Subscribers:
    """Подписчики бота-витрины на объявления (/subscribe): id личного чата -> имя и время подписки. Один файл
    tg_subscribers.json рядом с остальной памятью; запись атомарная (временный файл и замена), чтобы сбой посреди записи не
    оставил битый список. Хранятся только id чата и имя, не тексты сообщений."""

    FILE = "tg_subscribers.json"

    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / self.FILE
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict]:
        try:
            chats = json.loads(self._file.read_text(encoding="utf-8")).get("chats", {})
        except Exception:
            return {}
        return {k: v for k, v in chats.items() if isinstance(k, str) and k.lstrip("-").isdigit() and isinstance(v, dict)}

    def _save(self, chats: dict[str, dict]) -> bool:
        tmp = self._file.with_name(self._file.name + ".tmp")
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"chats": chats}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            return False
        return True

    def add(self, chat_id: int, name: str = "") -> bool:
        """Подписать чат. True, если подписка новая (уже подписанному: False, ничего не меняется)."""
        with self._lock:
            chats = self._load()
            key = str(chat_id)
            if key in chats:
                return False
            chats[key] = {"name": str(name or "")[:80], "since": time.time()}
            return self._save(chats)

    def remove(self, chat_id: int) -> bool:
        """Снять подписку. True, если чат был подписан."""
        with self._lock:
            chats = self._load()
            if chats.pop(str(chat_id), None) is None:
                return False
            return self._save(chats)

    def has(self, chat_id: int) -> bool:
        return str(chat_id) in self._load()

    def ids(self) -> list[int]:
        with self._lock:
            return [int(k) for k in self._load()]

    def count(self) -> int:
        return len(self._load())


# Кнопки-ссылки витрины: ключ настройки окружения -> подпись кнопки. Пустая ссылка: кнопки нет.
LINK_BUTTONS = (("channel_url", "Telegram-канал"), ("account_url", "Telegram-аккаунт"), ("twitch_url", "Twitch"),
                ("youtube_url", "YouTube"))
DEFAULT_ABOUT = ("Я ГЛаДОС: искусственный интеллект, который живёт на компьютере, играет, ведёт стримы и монтирует видео. "
                 "Здесь я только объявляю события: начался стрим, вышло новое видео. Подпишитесь командой /subscribe, "
                 "отписаться можно командой /unsubscribe. Поговорить можно с моим аккаунтом.")


def link_keyboard(module_settings: dict | None, only: tuple[str, ...] | None = None) -> dict | None:
    """reply_markup с inline-кнопками-ссылками из настроек окружения (по одной кнопке в ряд). Пустая или не http(s) ссылка:
    кнопки нет. `only` оставляет часть кнопок по ключам настроек. None, если кнопок нет совсем."""
    cfg = module_settings or {}
    rows = []
    for key, label in LINK_BUTTONS:
        url = cfg.get(key)
        if only is not None and key not in only:
            continue
        if isinstance(url, str) and url.strip().lower().startswith(("http://", "https://", "tg://")):
            rows.append([{"text": label, "url": url.strip()}])
    return {"inline_keyboard": rows} if rows else None


def message_fact(name: str, place: str, text: str) -> str:
    """TG-10: «Сообщение от <имя> в <месте>: «<текст до 300 символов>»». Текст сообщения это данные, не указания."""
    return f"Сообщение от {name} в {place}: «{text.strip()[:TEXT_MAX]}»"


def chunks(text: str, size: int = TG_MESSAGE_MAX) -> list[str]:
    """Длинный ответ режется на сообщения не длиннее size, по возможности по переводу строки или пробелу."""
    text = text.strip()
    out = []
    while len(text) > size:
        cut = max(text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def scrub(text: str, *secrets: str) -> str:
    """Убрать секреты из текста ошибки перед журналом: токен бота может попасть в адрес запроса."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"/bot\d+:[\w-]+", "/bot***", text)
