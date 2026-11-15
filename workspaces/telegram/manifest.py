"""Манифест модуля Telegram (ТЗ 10, раздел 5). Описания инструментов перенесены из прежних окружений social и
telegram_account (environments/builtin.py ядра) дословно: их читает планировщик."""
import copy

_GENERIC = ("телеграм", "телега", "тг", "канал", "пост", "написать", "чат")      # TG-8: общие слова просьб про Telegram


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False,
          danger_check=False, timeout_s=30):
    tool = {"name": name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if danger_check:
        tool["danger_check"] = True
    return tool


def _bool(description, required=True):
    spec = {"type": "boolean", "description": description}
    if not required:
        spec["required"] = False
    return spec


_LIMIT = "сколько {}, по умолчанию {}"

TOOLS = [
    _tool("message", "Отправить сообщение: в их канал (to=channel, по умолчанию), владельцу (to=owner) или с её аккаунта в чат "
                     "(название, @имя, id). Нужен text или topic (текст напишет сама); files: ресурсы ядра, снимки, last. "
                     "В незнакомый чат спрашивает разрешение.",
          {"to": {"type": "string", "required": False, "description": "channel (по умолчанию), owner или чат: название, @имя, id"},
           "text": {"type": "string", "required": False, "description": "готовый текст (подпись, если один файл: до 1024 символов)"},
           "topic": {"type": "string", "required": False, "description": "о чём написать, если готового текста нет"},
           "files": {"type": "array", "required": False,
                     "description": "список: идентификаторы из core.resources, имена снимков или last; "
                                    ".png/.jpg/.webp фото, .mp4 видео (до 50 МБ), .ogg/.opus голосовое, прочее документ"},
           "voice": {"type": "boolean", "required": False,
                     "description": "true: озвучить text её голосом; для канала по умолчанию как в настройках"},
           "reply_to": {"type": "integer", "required": False, "description": "id сообщения, на которое ответить"},
           "after_run": {"type": "boolean", "required": False,
                         "description": "true: вызов от игры после забега; публикуется, только если включена настройка "
                                        "«Пост-итог после забега»"}},
          informative=True, danger_check=True, timeout_s=120,
          keywords=("напиши", "отправь", "опубликуй", "запости", "выложи", "ответь", "видео", "скриншот", "сообщение")),
    _tool("inbox", "Читать Telegram её аккаунта: без параметров список диалогов с числом непрочитанных; chat - последние "
                   "сообщения чата (название, @имя или id); unread=true - что писали недавно.",
          {"chat": {"type": "string", "required": False, "description": "название чата, @имя или id"},
           "unread": _bool("true: что писали в её аккаунт недавно", required=False),
           "limit": {"type": "integer", "required": False, "description": _LIMIT.format("записей", 15)}},
          informative=True, timeout_s=60, keywords=("прочитай", "переписка", "сообщения", "чаты", "диалоги", "новые", "входящие")),
    _tool("settings", "Включить или выключить периодические посты-мысли в канал (periodic) и пост-итог после забега (after_run).",
          {"periodic": _bool("true: публиковать периодически", required=False),
           "after_run": _bool("true: писать итог после забега, когда колония погибла", required=False)},
          keywords=("публикуй", "мысли", "посты", "забег", "итог")),
]

MANIFEST = {
    "id": "telegram",
    "role": "workspace",
    "ownership": "owned",
    "title": "Telegram",
    "description": "Telegram: сообщения и посты в канал, её собственный аккаунт (чтение и отправка), разговор владельца с ней "
                   "и ответы незнакомым людям через бота.",
    "version": "1.0.0",
    "capabilities": ["chat.channel"],
    "subscribe": ["settings", "presence", "module", "character"],
    "shared_settings": ["quiet_until", "dnd"],
    "settings_schema": {
        "publish": {"type": "boolean", "title": "Публиковать мысли в канал (токен бота и канал задаются в .env)",
                    "default": False},
        "after_run": {"type": "boolean", "title": "Пост-итог после каждого забега (когда колония погибает)",
                      "default": False},
        "voice": {"type": "boolean", "title": "Посты голосом (текст остаётся подписью)", "default": False},
        "owner": {"type": "string", "title": "Аккаунт владельца в Telegram: @имя или числовой id", "default": ""},
        "owner_tg_id": {"type": "integer", "title": "Telegram id владельца (0: не задан; можно указать аккаунт выше)",
                        "default": 0, "min": 0},
        "every_mins": {"type": "integer", "title": "Как часто думать о посте, минут", "default": 180, "min": 30,
                       "max": 1440},
        "post_hours_from": {"type": "integer", "title": "Публиковать с часа", "default": 9, "min": 0, "max": 23},
        "post_hours_to": {"type": "integer", "title": "Публиковать до часа", "default": 23, "min": 0, "max": 23},
    },
    "tools": TOOLS,
    "emits": [
        {"kind": "telegram.bot_message", "description": "Личное сообщение боту от человека", "default_urgency": 2},
        {"kind": "telegram.dm", "description": "Личное сообщение в её аккаунт", "default_urgency": 3},
        {"kind": "telegram.group", "description": "Сообщение в группе её аккаунта", "default_urgency": 1},
        {"kind": "telegram.posted", "description": "Опубликован пост в канале", "default_urgency": 1},
    ],
    "state": True,
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): канал, аккаунт, последние диалоги без текста сообщений
    "panel": {"title": "Telegram", "icon": "send", "order": 60},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
