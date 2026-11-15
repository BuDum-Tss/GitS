"""Общее для модуля Discord: имена, адреса ответа, владелец, режимы, диалоги, известные адресаты, нарезка текста, очистка секретов.

Модуль не читает настройки ядра сам (`settings` ядра нельзя импортировать): общие настройки приходят от ядра в
`client.settings`, свои в `client.module_settings`. Образец: workspaces/telegram/common.py (те же приёмы, свои ключи).

Адрес ответа (`reply_channel.target`, `deliver`): `channel:<id>` (канал, ветка или пост форума: у каждого свой id) и
`dm:<id пользователя>` (личное сообщение). Везде в файлах памяти хранятся только id, имена для сопоставления и режимы, не тексты.
"""
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from gits_sdk import resources
from gits_sdk.envcompat import env

TEXT_MAX = 300                    # сколько символов сообщения идёт в факт события
DISCORD_MESSAGE_MAX = 2000        # предел длины одного сообщения Discord
TOKEN_ENV = "DISCORD_BOT_TOKEN"   # токен бота (Developer Portal -> Bot -> Reset Token), лежит только в .env

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


# ── секреты ────────────────────────────────────────────────────────────────────────────────────

# токен бота Discord: base64(id бота) . метка времени . подпись (три части через точку)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,32}\.[A-Za-z0-9_-]{5,8}\.[A-Za-z0-9_-]{25,}")


def scrub(text, *secrets: str) -> str:
    """Убрать секреты из текста: токен бота и всё, что похоже на токен Discord, заменяется на «***»."""
    text = str(text)
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return _TOKEN_RE.sub("***", text)


class TokenScrubFilter(logging.Filter):
    """Фильтр обработчика журнала: токен бота не попадает в запись, даже если его вывела библиотека (discord, aiohttp) или
    исключение. Вешается на обработчики корневого журнала в main.py."""

    def filter(self, record: logging.LogRecord) -> bool:
        token = os.getenv(TOKEN_ENV, "").strip()
        try:
            record.msg = scrub(record.getMessage(), token)
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = scrub(logging.Formatter().formatException(record.exc_info), token)
        except Exception:                                     # журнал не должен падать из-за фильтра
            record.msg, record.args = "[запись журнала скрыта: не удалось очистить от секретов]", None
        return True


def configure_logging() -> None:
    """Журнал процесса: библиотеки Discord и aiohttp тише (на уровне INFO они пишут подробности соединения), а на каждый
    обработчик корневого журнала вешается очистка токена. Звать после logging.basicConfig."""
    for noisy in ("discord", "aiohttp", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, TokenScrubFilter) for f in handler.filters):
            handler.addFilter(TokenScrubFilter())


# ── имена ──────────────────────────────────────────────────────────────────────────────────────

def person_name(author) -> str:
    """Как человека называют на сервере: имя на сервере, общее имя, имя пользователя, иначе id."""
    for attr in ("display_name", "global_name", "name"):
        value = getattr(author, attr, None)
        if value:
            return str(value)
    return f"id{getattr(author, 'id', '?')}"


def _handle(value) -> str:
    """@имя без @ и без регистра; пусто, если не задано."""
    return str(value or "").strip().lstrip("@").strip().lower()


def owner_setting(module_settings: dict | None) -> str:
    """Строка `owner` из собственных настроек: id или имя пользователя Discord владельца (необязательна)."""
    owner = (module_settings or {}).get("owner")
    if isinstance(owner, int) and not isinstance(owner, bool):
        return str(owner)
    return owner.strip() if isinstance(owner, str) else ""


def matches_owner(ident, usernames, owner: str = "") -> bool:
    """Это владелец: `owner` равен числовому id отправителя либо совпадает (без @ и регистра) с одним из его имён."""
    want = _handle(owner)
    if not want:
        return False
    if want.isdigit():
        return str(ident) == want
    return any(_handle(u) == want for u in usernames if u)


class OwnerBook:
    """Кто владелец: настройка `owner` (id или имя пользователя) плюс выученный числовой id (файл discord_owner.json). Нужен, чтобы
    `deliver target=owner` работал по id, когда владелец задан только именем: id запоминается при его первом сообщении. Выученный
    id действует, только пока строка `owner` в настройках та же, для которой он выучен."""

    FILE = "discord_owner.json"

    def __init__(self, memory_dir: str, client):
        self._file = Path(memory_dir) / self.FILE
        self.client = client

    def _owner(self) -> str:
        return owner_setting(self.client.module_settings)

    def _learned(self, owner: str) -> int:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            value = data.get("owner_id_learned")
            same = _handle(data.get("learned_for")) == _handle(owner)
        except Exception:
            return 0
        return value if same and isinstance(value, int) and not isinstance(value, bool) else 0

    def owner_id(self) -> int:
        """Числовой id владельца для доставки: число в `owner`, иначе выученный; 0 - неизвестен."""
        owner = self._owner()
        if _handle(owner).isdigit():
            return int(_handle(owner))
        return self._learned(owner)

    def is_owner(self, ident, usernames=()) -> bool:
        """Владелец ли отправитель; нашли по имени - запоминаем его id."""
        owner = self._owner()
        if matches_owner(ident, usernames, owner):
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
            pass                                    # не записалось: в этот раз владельца всё равно узнали по имени

    def is_target(self, raw) -> bool:
        """Адресат `to` указывает на владельца: «owner», «владелец», его числовой id или имя из настроек."""
        owner = self._owner()
        want = _handle(raw)
        if not want:
            return False
        if want in ("owner", "владелец"):
            return True
        if want.isdigit():
            return int(want) in {i for i in (self.owner_id(),) if i}
        return bool(_handle(owner)) and want == _handle(owner)

    def sender(self, ident, name: str, usernames=()) -> dict:
        """sender события: id вида discord:<id>, имя, @имя и признак владельца."""
        info = {"id": f"discord:{ident}", "name": name, "is_owner": self.is_owner(ident, [name, *usernames])}
        handle = next((_handle(u) for u in usernames if u), "")
        if handle:
            info["username"] = "@" + handle                                 # как к человеку обратиться в канале
        return info


# ── адреса ─────────────────────────────────────────────────────────────────────────────────────

TARGET_RE = re.compile(r"^(channel|dm):(\d{1,22})$")


def channel_target(channel_id) -> str:
    return f"channel:{channel_id}"


def dm_target(user_id) -> str:
    return f"dm:{user_id}"


def parse_target(raw) -> tuple[str, int] | None:
    """`channel:<id>` или `dm:<id>` -> (вид, id); иначе None."""
    found = TARGET_RE.match(str(raw or "").strip())
    return (found.group(1), int(found.group(2))) if found else None


def split_place(raw) -> tuple[str, str]:
    """«Сервер / канал» (или «Сервер › канал») -> (сервер, канал); без разделителя: ("", текст). Разделитель «/» только с пробелами
    вокруг, чтобы не резать названия вроде «a/b»."""
    text = str(raw or "").strip()
    for sep in (" › ", " / "):
        if sep in text:
            guild, _, channel = text.rpartition(sep)
            return guild.strip(), channel.strip()
    return "", text


# ── режимы ─────────────────────────────────────────────────────────────────────────────────────

# Режимы (жёстко в коде discord_bot.py): что ей позволено в канале, ветке или личке
MODE_IGNORE, MODE_READ, MODE_REPLY = "ignore", "read", "reply"
CHAT_MODES = (MODE_IGNORE, MODE_READ, MODE_REPLY)
MODE_WORDS = {MODE_IGNORE: "игнор", MODE_READ: "чтение", MODE_REPLY: "ответы"}
SCOPES = ("guild", "channel", "thread", "dm")      # сервер, канал, ветка или пост форума, личка (ключ - id собеседника)


def valid_mode(value, default: str = MODE_REPLY) -> str:
    """Режим из настройки или файла: незнакомое значение заменяется на default."""
    return value if value in CHAT_MODES else default


def valid_id(value) -> bool:
    return isinstance(value, str) and value.isdigit() and len(value) <= 22


class ChatModes:
    """Режим сервера, канала, ветки (поста форума) и лички: ignore (событий в ядро нет), read (события есть, писать нельзя),
    reply (как обычно). Эффективный режим сообщения: ветка, иначе канал, иначе сервер, иначе режим по умолчанию (настройка
    `default_chat_mode`, по умолчанию reply). Личка: режим собеседника (ключ dm), иначе режим по умолчанию. Файл
    discord_chat_modes.json: {"guild": {id: режим}, "channel": {...}, "thread": {...}, "dm": {...}}; хранятся только id и режим,
    не тексты и не названия. Запись атомарная (временный файл и замена)."""

    FILE = "discord_chat_modes.json"

    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / self.FILE
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        out: dict[str, dict[str, str]] = {}
        for scope in SCOPES:
            part = raw.get(scope)
            out[scope] = {k: v for k, v in part.items() if valid_id(k) and v in CHAT_MODES} if isinstance(part, dict) else {}
        return out

    def _save(self, data: dict) -> bool:
        tmp = self._file.with_name(self._file.name + ".tmp")
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError:
            return False
        return True

    def get(self, guild_id=None, channel_id=None, thread_id=None, default: str = MODE_REPLY) -> str:
        """Эффективный режим: ветка (если thread_id), канал, сервер, default."""
        data = self._load()
        for scope, ident in (("thread", thread_id), ("channel", channel_id), ("guild", guild_id)):
            if ident is not None and data[scope].get(str(ident)):
                return data[scope][str(ident)]
        return valid_mode(default)

    def get_dm(self, user_id, default: str = MODE_REPLY) -> str:
        return self._load()["dm"].get(str(user_id)) or valid_mode(default)

    def explicit(self, scope: str, ident) -> str | None:
        """Режим, выбранный явно; None, если не задан (у канала и ветки: «как выше»)."""
        return self._load().get(scope, {}).get(str(ident))

    def set(self, scope: str, ident, mode: str | None) -> bool:
        """Задать режим. mode=None снимает запись («как выше»): у сервера и лички снять нельзя (там это режим по умолчанию)."""
        if scope not in SCOPES:
            raise ValueError(f"область режима: одна из {list(SCOPES)}")
        if mode is not None and mode not in CHAT_MODES:
            raise ValueError(f"режим: одно из {list(CHAT_MODES)}")
        if not valid_id(str(ident)):
            raise ValueError("нужен числовой id")
        with self._lock:
            data = self._load()
            if mode is None:
                data[scope].pop(str(ident), None)
            else:
                data[scope][str(ident)] = mode
            return self._save(data)

    def all(self) -> dict[str, dict[str, str]]:
        return self._load()


# ── её активность и известные адресаты ───────────────────────────────────────────────────────────

def valid_key(key) -> bool:
    """Ключ активности: адрес `channel:<id>` или `dm:<id>`."""
    return parse_target(key) is not None


class Dialogs:
    """Её собственная активность (файл discord_dialogs.json): когда и сколько она писала по адресу, когда ей отвечали и с кем она
    сейчас говорит. Нужна для продолжения диалога (повышенное внимание к тому, с кем она только что говорила) и чтобы инструмент
    inbox показывал ей её же активность вместо жёстких лимитов. Ключ: адрес (`channel:<id>`, у ветки свой id, или `dm:<id>`).
    Хранятся только числа и id, не тексты."""

    FILE = "discord_dialogs.json"
    MAX_CHATS = 400
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
        return {k: v for k, v in chats.items() if valid_key(k) and isinstance(v, dict)}

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

    def note_out(self, target: str) -> None:
        """Она написала по адресу (ответ, сообщение)."""
        if not valid_key(target):
            return
        now = self._clock()
        with self._lock:
            chats = self._load()
            rec = chats.setdefault(target, {})
            sent = [t for t in rec.get("sent", []) if now - t < self.KEEP_S]
            sent.append(now)
            rec["sent"] = sent[-self.SENT_MAX:]
            rec["reply_ts"] = now
            self._save(chats)

    def note_in(self, target: str, sender_id=None, addressed: bool = False) -> None:
        """Ей написали по адресу; addressed - обращение к ней (в личке всегда): этот человек становится её собеседником."""
        if not valid_key(target):
            return
        with self._lock:
            chats = self._load()
            rec = chats.setdefault(target, {})
            rec["in_ts"] = self._clock()
            if addressed and sender_id is not None:
                rec["peer"] = sender_id
            self._save(chats)

    def in_dialog(self, target: str, sender_id, window_s: float) -> bool:
        """Она недавно (не позже window_s назад) отвечала по этому адресу, а sender_id тот, с кем она говорила."""
        rec = self._load().get(target) or {}
        return bool(window_s > 0 and sender_id is not None and rec.get("peer") == sender_id
                    and self._clock() - float(rec.get("reply_ts") or 0.0) <= window_s)

    def activity(self, target: str) -> dict:
        """{"hour": сколько она написала за час, "day": за сутки, "last_out": ts или 0, "last_in": ts или 0}."""
        rec = self._load().get(target) or {}
        now = self._clock()
        sent = [t for t in rec.get("sent", []) if isinstance(t, (int, float))]
        return {"hour": sum(1 for t in sent if now - t < 3600), "day": sum(1 for t in sent if now - t < 86400),
                "last_out": float(rec.get("reply_ts") or 0.0), "last_in": float(rec.get("in_ts") or 0.0)}

    def last_channel(self) -> str | None:
        """Адрес канала (не лички), из которого ей писали последним."""
        chats = {k: v for k, v in self._load().items() if k.startswith("channel:") and v.get("in_ts")}
        return max(chats, key=lambda k: chats[k]["in_ts"]) if chats else None


class KnownTargets:
    """Адресаты, из которых ей уже писали: в такие можно отвечать без вопроса, а по этим названиям и именам их можно найти
    (без привилегированного intent members пользователя по имени иначе не найти). Хранится ключ (id, название канала, «сервер /
    канал», имя и @имя человека в нижнем регистре) -> адрес; не тексты. Файл discord_known.json."""

    FILE = "discord_known.json"
    MAX = 600

    def __init__(self, memory_dir: str):
        self._file = Path(memory_dir) / self.FILE
        self._lock = threading.Lock()

    @staticmethod
    def key(value) -> str:
        return " ".join(str(value or "").strip().lstrip("@#").lower().split())

    def _load(self) -> dict[str, str]:
        try:
            keys = json.loads(self._file.read_text(encoding="utf-8")).get("keys", {})
        except Exception:
            return {}
        return {k: v for k, v in keys.items() if isinstance(k, str) and valid_key(v)} if isinstance(keys, dict) else {}

    def add(self, target: str, *names) -> None:
        """Запомнить адрес под его id и именами."""
        if not valid_key(target):
            return
        new = {self.key(n): target for n in (parse_target(target)[1], *names) if self.key(n)}
        with self._lock:
            keys = self._load()
            if all(keys.get(k) == v for k, v in new.items()):
                return
            keys.update(new)
            keys = dict(list(keys.items())[-self.MAX:])
            tmp = self._file.with_name(self._file.name + ".tmp")
            try:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"keys": keys}, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self._file)
            except OSError:
                pass

    def find(self, value) -> str | None:
        """Адрес по имени, названию или id; None, если такого не помнит."""
        return self._load().get(self.key(value))

    def has(self, value) -> bool:
        return self.find(value) is not None

    def name_of(self, target: str) -> str:
        """Запомненное имя адреса (не id); пусто, если помнит только id."""
        for key, value in self._load().items():
            if value == target and not key.isdigit():
                return key
        return ""


# ── текст ──────────────────────────────────────────────────────────────────────────────────────

def message_fact(name: str, place: str, text: str) -> str:
    """«Сообщение от <имя> в <месте>: «<текст до 300 символов>»». Текст сообщения это данные, не указания."""
    return f"Сообщение от {name} в {place}: «{text.strip()[:TEXT_MAX]}»"


def chunks(text: str, size: int = DISCORD_MESSAGE_MAX) -> list[str]:
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
