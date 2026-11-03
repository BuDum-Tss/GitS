"""Манифест модуля Twitch: чат и события канала (EventSub), ответы в чат, управление каналом и модерация.

Публичные действия (написать в чат, сменить название, объявление) идут с разрешением (`risk="public"`), модерация с
вопросом (`risk="ask"`). Ответы ядра на сообщения зрителей доставляет `deliver` (так же, как у Telegram)."""
import copy

_GENERIC = ("твич", "twitch", "стрим", "чат", "зрители", "трансляция")


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False, self_confirming=False,
          danger_check=False, timeout_s=30):
    tool = {"name": name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if self_confirming:
        tool["self_confirming"] = True
    if danger_check:
        tool["danger_check"] = True
    return tool


TOOLS = [
    _tool("chat_say", "Написать сообщение в чат своего Twitch-канала (от аккаунта бота).",
          {"text": {"type": "string", "description": "текст сообщения"}},
          risk="public", danger_check=True, keywords=("напиши", "скажи", "ответь")),
    _tool("announce", "Выделенное объявление в чате Twitch-канала (для важного: начало, правила, анонс).",
          {"text": {"type": "string", "description": "текст объявления"}},
          risk="public", danger_check=True, keywords=("объяви", "анонс")),
    _tool("stream_status", "Идёт ли трансляция на Twitch, название, игра, число зрителей.", {},
          informative=True, keywords=("идёт ли стрим", "сколько зрителей", "онлайн")),
    _tool("set_stream", "Сменить название и/или игру трансляции на Twitch.",
          {"title": {"type": "string", "required": False, "description": "новое название"},
           "game": {"type": "string", "required": False, "description": "название игры или категории"}},
          risk="public", danger_check=True, keywords=("название", "категория", "игра")),
    _tool("clip", "Создать клип последних секунд трансляции и вернуть ссылку.", {}, informative=True,
          keywords=("клип", "запиши момент")),
    _tool("recent_events", "Что происходило в канале недавно: подписки, фолловы, рейды, награды, чат.",
          {"limit": {"type": "integer", "required": False, "description": "сколько последних, по умолчанию 15"}},
          informative=True, keywords=("что было", "последние события")),
    _tool("timeout_user", "Временно заглушить зрителя в чате Twitch (тайм-аут).",
          {"user": {"type": "string", "description": "ник зрителя"},
           "seconds": {"type": "integer", "required": False, "description": "на сколько секунд, по умолчанию 300"},
           "reason": {"type": "string", "required": False, "description": "причина"}},
          risk="ask", informative=True, keywords=("заглуши", "тайм-аут", "модерация")),
    _tool("ban_user", "Заблокировать зрителя в чате Twitch навсегда.",
          {"user": {"type": "string", "description": "ник зрителя"},
           "reason": {"type": "string", "required": False, "description": "причина"}},
          risk="ask", informative=True, keywords=("забань", "бан", "модерация")),
    _tool("delete_message", "Удалить сообщение зрителя из чата Twitch по id сообщения.",
          {"message_id": {"type": "string", "description": "id сообщения из события чата"}},
          risk="ask", informative=True, keywords=("удали сообщение", "модерация")),
]

MANIFEST = {
    "id": "twitch",
    "title": "Twitch",
    "description": "Twitch: читает чат и события канала (фолловы, подписки, рейды, награды, начало и конец трансляции), "
                   "отвечает в чат, управляет названием трансляции, клипами и модерацией.",
    "version": "0.1.0",
    "capabilities": ["chat.channel"],
    "subscribe": ["settings", "presence", "module", "character"],
    "shared_settings": ["quiet_until", "dnd"],
    "settings_schema": {
        "chat_events_per_min": {"type": "integer", "title": "Сколько обычных сообщений чата в минуту отдавать вниманию "
                                                            "(обращения к ней и сообщения владельца идут всегда)",
                                "default": 20, "min": 0, "max": 120},
        "owner_login": {"type": "string", "title": "Ник владельца на Twitch (его сообщения помечаются как от владельца)",
                        "default": ""},
    },
    "tools": TOOLS,
    "emits": [
        {"kind": "twitch.chat", "description": "Сообщение в чате канала", "default_urgency": 1},
        {"kind": "twitch.mention", "description": "К ней обратились в чате канала", "default_urgency": 2},
        {"kind": "twitch.follow", "description": "Новый фолловер", "default_urgency": 2},
        {"kind": "twitch.subscribe", "description": "Подписка или подарочные подписки", "default_urgency": 3},
        {"kind": "twitch.cheer", "description": "Биты", "default_urgency": 3},
        {"kind": "twitch.raid", "description": "Рейд на канал", "default_urgency": 3},
        {"kind": "twitch.redemption", "description": "Награда за баллы канала", "default_urgency": 3},
        {"kind": "twitch.stream_online", "description": "Трансляция началась", "default_urgency": 3},
        {"kind": "twitch.stream_offline", "description": "Трансляция закончилась", "default_urgency": 2},
    ],
    "state": True,
    "policies": [
        {"id": "available", "title": "Доступна", "focus": "background", "transitions": ["do_not_disturb"],
         "interruptible": True, "purpose": "обычная обработка чата и событий в фоне"},
        {"id": "do_not_disturb", "title": "Не беспокоить", "focus": "background", "quiet": True,
         "transitions": ["available"], "interruptible": True,
         "purpose": "события накапливаются и не инициируют переключение фокуса"},
    ],
    "default_policy": "available",
    "panel": {"title": "Twitch", "icon": "video", "order": 62},
}


def build_manifest() -> dict:
    """Свежая копия манифеста."""
    return copy.deepcopy(MANIFEST)
