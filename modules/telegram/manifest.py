"""Манифест модуля Telegram (ТЗ 10, раздел 5). Описания инструментов перенесены из прежних окружений social и
telegram_account (environments/builtin.py ядра) дословно: их читает планировщик."""
import copy

_GENERIC = ("телеграм", "телега", "тг", "канал", "пост", "написать", "чат")      # TG-8: общие слова просьб про Telegram


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


def _bool(description, required=True):
    spec = {"type": "boolean", "description": description}
    if not required:
        spec["required"] = False
    return spec


_VOICE = {"type": "boolean", "required": False,
          "description": "true: голосовым сообщением её голосом; по умолчанию как в настройках"}
_LIMIT = "сколько {}, по умолчанию {}"

TOOLS = [
    _tool("post", "Написать и опубликовать пост в Telegram-канал на заданную тему (если тема не названа, приветственный пост).",
          {"topic": {"type": "string", "description": "о чём пост, своими словами пользователя", "required": False},
           "voice": dict(_VOICE)},
          risk="public", self_confirming=True, informative=True, timeout_s=120,
          keywords=("опубликуй", "запости", "выложи")),
    _tool("draft_show", "Показать текущий черновик поста для канала (текст и приложенные снимки).", {},
          informative=True, keywords=("черновик", "драфт", "покажи")),
    _tool("draft_write", "Написать черновик поста для Telegram-канала (заменяет текущий текст). "
                         "Можно дать готовый text или тему topic: тогда она напишет сама, с учётом событий игры.",
          {"text": {"type": "string", "required": False, "description": "готовый текст поста"},
           "topic": {"type": "string", "required": False, "description": "о чём написать"}},
          informative=True, timeout_s=100, keywords=("черновик", "драфт", "напиши", "заметка")),
    _tool("draft_edit", "Переписать текущий черновик по указанию («короче», «добавь шутку», «убери вторую фразу»).",
          {"instruction": {"type": "string", "description": "что изменить в черновике"}},
          informative=True, timeout_s=100, keywords=("отредактируй", "поправь", "перепиши", "короче", "добавь", "убери",
                                                     "черновик", "драфт")),
    _tool("draft_append", "Дописать готовый текст в конец черновика.",
          {"text": {"type": "string", "description": "что дописать"}}, keywords=("допиши", "добавь", "черновик")),
    _tool("draft_replace", "Точная замена фрагмента в черновике: первое вхождение find меняется на replace.",
          {"find": {"type": "string", "description": "что заменить"}, "replace": {"type": "string", "description": "на что"}},
          keywords=("замени", "исправь", "черновик")),
    _tool("draft_attach", "Приложить скриншот к черновику: shot — имя файла или last (последний снимок, по умолчанию).",
          {"shot": {"type": "string", "required": False, "description": "имя файла снимка или last"}},
          keywords=("приложи", "вставь", "скриншот", "снимок", "картинка", "черновик")),
    _tool("draft_detach", "Убрать снимок из черновика (shot — имя файла или all).",
          {"shot": {"type": "string", "required": False, "description": "имя файла или all"}},
          keywords=("убери", "сними", "скриншот", "черновик")),
    _tool("draft_clear", "Выбросить черновик целиком (текст и снимки).", {}, keywords=("очисти", "выброси", "удали", "черновик")),
    _tool("draft_publish", "Опубликовать черновик в Telegram-канал (текст и приложенные снимки).",
          {"voice": dict(_VOICE)}, risk="public", self_confirming=True, informative=True, timeout_s=120,
          keywords=("опубликуй", "выложи", "запости", "отправь", "черновик")),
    _tool("chats", "Список диалогов её Telegram-аккаунта (личные, группы, каналы) с числом непрочитанных.",
          {"limit": {"type": "integer", "required": False, "description": _LIMIT.format("диалогов", 20)}},
          informative=True, timeout_s=60, keywords=("чаты", "диалоги", "переписка", "аккаунт")),
    _tool("read", "Прочитать последние сообщения чата её Telegram-аккаунта (чат по названию, @имени или id).",
          {"chat": {"type": "string", "description": "название чата, @имя или id"},
           "limit": {"type": "integer", "required": False, "description": _LIMIT.format("сообщений", 15)}},
          informative=True, timeout_s=60, keywords=("прочитай", "переписка", "сообщения")),
    _tool("unread", "Что писали в её Telegram-аккаунт недавно (входящие, которые она заметила).",
          {"limit": {"type": "integer", "required": False, "description": _LIMIT.format("последних", 15)}},
          informative=True, keywords=("новые", "входящие", "написали", "сообщения", "что мне писали")),
    _tool("send", "Написать сообщение с её Telegram-аккаунта в чат (по названию, @имени или id). "
                  "Каждый раз спрашивает разрешение.",
          {"chat": {"type": "string", "description": "название чата, @имя или id"},
           "text": {"type": "string", "description": "текст сообщения"}},
          risk="ask", informative=True, danger_check=True, timeout_s=60,
          keywords=("напиши", "отправь", "ответь", "сообщение")),
    _tool("set_periodic", "Включить или выключить периодические посты-мысли в Telegram-канал.",
          {"enabled": _bool("true - публиковать периодически, false - не публиковать")},
          keywords=("публикуй", "мысли", "посты")),
    _tool("set_after_run", "Включить или выключить пост-итог в Telegram-канал после каждого "
                           "забега (когда колония погибла). Для просьб «после каждого забега пиши пост».",
          {"enabled": _bool("true - писать итог после забега, false - не писать")},
          keywords=("забег", "после", "итог", "колония")),
]

MANIFEST = {
    "id": "telegram",
    "title": "Telegram",
    "description": "Telegram: посты бота в канал, черновики, её собственный аккаунт (чаты, чтение, отправка) и разговор "
                   "владельца с ней через бота и личку.",
    "version": "1.0.0",
    "capabilities": ["chat.channel"],
    "subscribe": ["settings", "presence", "module", "character"],
    "shared_settings": ["telegram", "telegram_voice", "telegram_confirm", "quiet_until", "dnd"],
    "settings_schema": {
        "owner_tg_id": {"type": "integer", "title": "Telegram id владельца (0: разговор через Telegram выключен)",
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
    # политики фокуса (ТЗ 18, FOC-1; ТЗ 11e, POL-13): канал не держит фокус, обычные сообщения его не переключают;
    # «не беспокоить» - режим без переключений (сообщения копятся и оцениваются позже)
    "policies": [
        {"id": "available", "title": "Доступна", "focus": "background", "transitions": ["do_not_disturb"],
         "interruptible": True, "purpose": "обычная обработка сообщений в фоне"},
        {"id": "do_not_disturb", "title": "Не беспокоить", "focus": "background", "quiet": True,
         "transitions": ["available"], "interruptible": True,
         "purpose": "сообщения накапливаются и не инициируют переключение фокуса"},
    ],
    "default_policy": "available",
    # пространство в дашборде (TZ 15, TAB-6/TAB-9): канал, аккаунт, последние диалоги без текста сообщений
    "panel": {"title": "Telegram", "icon": "send", "order": 60},
}


def build_manifest() -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою)."""
    return copy.deepcopy(MANIFEST)
