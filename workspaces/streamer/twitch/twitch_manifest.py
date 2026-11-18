"""Манифест подсистемы Twitch (часть окружения «Стример»): чат и события канала (EventSub), ответы в чат, управление каналом и модерация.

Публичные действия (написать в чат, сменить название, объявление) идут с разрешением (`risk="public"`), модерация с
вопросом (`risk="ask"`). Ответы ядра на сообщения зрителей доставляет `deliver` (так же, как у Telegram)."""
PREFIX = "twitch_"                    # имена инструментов подсистемы: streamer.twitch_<имя>

_GENERIC = ("твич", "twitch", "стрим", "чат", "зрители", "трансляция")


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False, self_confirming=False,
          danger_check=False, timeout_s=30, read_only=False):
    tool = {"name": PREFIX + name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if self_confirming:
        tool["self_confirming"] = True
    if danger_check:
        tool["danger_check"] = True
    if read_only:
        tool["read_only"] = True          # явное чтение: журнал действий хода агента не считает его действием с эффектом
    return tool


TOOLS = [
    _tool("chat_say", "Написать сообщение в чат своего Twitch-канала (от аккаунта бота).",
          {"text": {"type": "string", "description": "текст сообщения"}},
          risk="public", danger_check=True, keywords=("напиши", "скажи", "ответь")),
    _tool("announce", "Выделенное объявление в чате Twitch-канала (для важного: начало, правила, анонс).",
          {"text": {"type": "string", "description": "текст объявления"}},
          risk="public", danger_check=True, keywords=("объяви", "анонс")),
    _tool("stream_status", "Идёт ли трансляция на Twitch, название, игра, число зрителей.", {},
          informative=True, read_only=True, keywords=("идёт ли стрим", "сколько зрителей", "онлайн")),
    _tool("set_stream", "Сменить название и/или игру трансляции на Twitch.",
          {"title": {"type": "string", "required": False, "description": "новое название"},
           "game": {"type": "string", "required": False, "description": "название игры или категории"}},
          risk="public", danger_check=True, keywords=("название", "категория", "игра")),
    _tool("clip", "Создать клип последних секунд трансляции и вернуть ссылку.", {}, informative=True,
          keywords=("клип", "запиши момент")),
    _tool("recent_events", "Что происходило в канале недавно: подписки, фолловы, рейды, награды, чат.",
          {"limit": {"type": "integer", "required": False, "description": "сколько последних, по умолчанию 15"}},
          informative=True, read_only=True, keywords=("что было", "последние события")),
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

SETTINGS_SCHEMA = {
    "chat_events_per_min": {"type": "integer", "title": "Сколько обычных сообщений чата в минуту отдавать вниманию "
                                                        "(обращения к ней и сообщения владельца идут всегда)",
                            "default": 20, "min": 0, "max": 120},
    "owner_login": {"type": "string", "title": "Ник владельца на Twitch (его сообщения помечаются как от владельца)",
                    "default": ""},
}

EMITS = [
    {"kind": "twitch.chat", "description": "Сообщение в чате канала", "default_urgency": 1},
    {"kind": "twitch.mention", "description": "К ней обратились в чате канала", "default_urgency": 2},
    {"kind": "twitch.follow", "description": "Новый фолловер", "default_urgency": 2},
    {"kind": "twitch.subscribe", "description": "Подписка или подарочные подписки", "default_urgency": 3},
    {"kind": "twitch.cheer", "description": "Биты", "default_urgency": 3},
    {"kind": "twitch.raid", "description": "Рейд на канал", "default_urgency": 3},
    {"kind": "twitch.redemption", "description": "Награда за баллы канала", "default_urgency": 3},
    {"kind": "twitch.stream_online", "description": "Трансляция началась", "default_urgency": 3},
    {"kind": "twitch.stream_offline", "description": "Трансляция закончилась", "default_urgency": 2},
]
