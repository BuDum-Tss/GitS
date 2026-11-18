"""Манифест модуля Telegram (ТЗ 10, раздел 5). Описания инструментов перенесены из прежних окружений social и
telegram_account (environments/builtin.py ядра) дословно: их читает планировщик."""
import copy

from common import CHAT_MODES, DEFAULT_ABOUT, MODE_REPLY
from telegram_panel import ACTIONS as PANEL_ACTIONS

_GENERIC = ("телеграм", "телега", "тг", "канал", "пост", "написать", "чат")      # TG-8: общие слова просьб про Telegram


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False,
          danger_check=False, timeout_s=30, sends=False, read_only=False):
    tool = {"name": name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if danger_check:
        tool["danger_check"] = True
    if sends:
        tool["sends"] = True              # отправляет текст людям: ядро пишет её исходящее событие module_out
    if read_only:
        tool["read_only"] = True          # явное чтение: журнал действий хода агента не считает его действием с эффектом
    return tool


def _bool(description, required=True):
    spec = {"type": "boolean", "description": description}
    if not required:
        spec["required"] = False
    return spec


_LIMIT = "сколько {}, по умолчанию {}"

_SEND_DESC = ("Отправить сообщение: в их канал (to=channel, по умолчанию), владельцу (to=owner) или с её аккаунта в чат "
              "(название, @имя, id; тема форума: «Чат / Тема»). Нужен text или topic (текст напишет сама); files: "
              "ресурсы ядра, снимки, last. В незнакомый чат спросит.")
_SEND_PARAMS = {
    "to": {"type": "string", "required": False, "description": "channel (по умолчанию), owner или чат: название, @имя, id; тема форума: «Чат / Название темы» или «Чат#id темы»"},
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
                                 "«Пост-итог после забега»"}}
_SEND_KEYWORDS = ("напиши", "отправь", "опубликуй", "запости", "выложи", "ответь", "видео", "скриншот", "сообщение")

TOOLS = [
    # text: явная дорожка текстового ответа (RUNTIME_SPEC 7.1), message: прежнее имя того же инструмента (алиас)
    _tool("text", "Основной способ ответить текстом. " + _SEND_DESC, copy.deepcopy(_SEND_PARAMS),
          informative=True, danger_check=True, timeout_s=120, sends=True, keywords=_SEND_KEYWORDS),
    _tool("message", "То же, что text. " + _SEND_DESC, copy.deepcopy(_SEND_PARAMS),
          informative=True, danger_check=True, timeout_s=120, sends=True, keywords=_SEND_KEYWORDS),
    _tool("inbox", "Читать Telegram её аккаунта: без параметров список диалогов с числом непрочитанных; chat - последние "
                   "сообщения чата (название, @имя или id) и сколько она сама в нём писала за час и сутки; unread=true - что писали недавно.",
          {"chat": {"type": "string", "required": False, "description": "название чата, @имя или id"},
           "unread": _bool("true: что писали в её аккаунт недавно", required=False),
           "limit": {"type": "integer", "required": False, "description": _LIMIT.format("записей", 15)}},
          informative=True, read_only=True, timeout_s=60, keywords=("прочитай", "переписка", "сообщения", "чаты", "диалоги", "новые", "входящие")),
    _tool("announce", "Разослать объявление подписчикам бота-витрины (подписка: /subscribe): начался стрим, вышло видео. "
                      "text - текст объявления, url - необязательная ссылка (добавится в конец). Заблокировавшие бота "
                      "снимаются с подписки.",
          {"text": {"type": "string", "description": "текст объявления (до 1000 символов, без личных данных)"},
           "url": {"type": "string", "required": False, "description": "ссылка на стрим или видео (http/https)"}},
          informative=True, timeout_s=120,
          keywords=("объяви", "объявление", "рассылка", "подписчикам", "анонс", "стрим", "вышло видео")),
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
    "description": "Telegram: её аккаунт (вход по QR из дашборда; личка, группы и темы форумов, режимы чатов и тем: игнор, чтение, ответы), "
                   "события telegram.dm и telegram.group (тег в группе обязателен к ответу, если режим «ответы»), посты в "
                   "канал, бот-витрина. Панель: чаты, переписка, ручная отправка владельцем.",
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
        "about": {"type": "string", "title": "Бот-витрина: описание в ответ на /start и /help",
                  "default": DEFAULT_ABOUT},
        "channel_url": {"type": "string", "title": "Бот-витрина: ссылка на её Telegram-канал (пусто: кнопки нет)", "default": ""},
        "account_url": {"type": "string", "title": "Бот-витрина: ссылка на её Telegram-аккаунт, где с ней можно говорить "
                                                    "(пусто: кнопки нет)", "default": ""},
        "twitch_url": {"type": "string", "title": "Бот-витрина: ссылка на Twitch (пусто: кнопки нет)", "default": ""},
        "youtube_url": {"type": "string", "title": "Бот-витрина: ссылка на YouTube (пусто: кнопки нет)", "default": ""},
        "names": {"type": "string", "title": "Как её зовут в группах: имена через запятую (к ней обратились, если имя есть в "
                                             "тексте; тег и ответ на её сообщение считаются всегда)", "default": ""},
        "dm_flood_per_10min": {"type": "integer", "title": "Лавина в личке: если один человек написал за 10 минут больше "
                               "стольких сообщений, следующие идут в сводку (0: выключено; ничего не блокируется)",
                               "default": 20, "min": 0, "max": 1000},
        "dialog_window_mins": {"type": "integer", "title": "Продолжение диалога: сколько минут после её ответа собеседник "
                               "считается говорящим с ней без тега (0: выключено)", "default": 15, "min": 0, "max": 240},
        "default_chat_mode": {"type": "string", "enum": list(CHAT_MODES), "default": MODE_REPLY,
                              "title": "Режим нового чата её аккаунта: ignore (событий нет), read (читает, не пишет), "
                                       "reply (читает и отвечает); у каждого чата меняется в панели"},
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
        {"kind": "telegram.dm", "description": "Личное сообщение в её аккаунт (чужому отвечает она сама, лавина идёт в сводку)", "default_urgency": 3},
        {"kind": "telegram.group", "description": "Сообщение в группе её аккаунта (тег или ответ ей: обязательный разговор, правило R6)", "default_urgency": 1},
        {"kind": "telegram.posted", "description": "Опубликован пост в канале", "default_urgency": 1},
    ],
    # виды для внимания v2 (RUNTIME_SPEC 3.3): приоритет USER ставит адаптер через addressed_to_me, сам вид потолок не режет
    "event_kinds": [
        {"kind": "telegram.dm", "default_priority": "IMPORTANT", "max_priority": "USER", "ttl_s": 900, "reply_tool": "telegram.text"},
        {"kind": "telegram.group", "default_priority": "NORMAL", "max_priority": "USER", "ttl_s": 900, "coalesce": True,
         "reply_tool": "telegram.text"},
        {"kind": "telegram.posted", "default_priority": "BACKGROUND", "max_priority": "NOTABLE", "ttl_s": 600},
    ],
    "state": True,
    # пространство в дашборде (TZ 15): вход в аккаунт, чаты с режимами, открытый чат; действия владельца (не инструменты агента)
    "panel": {"title": "Telegram", "icon": "send", "order": 60, "actions": list(PANEL_ACTIONS)},
}


def build_manifest(serve_dirs: list[str] | None = None) -> dict:
    """Свежая копия манифеста (клиент тоже копирует, но тестам удобно менять свою). `serve_dirs` - каталог с картинкой QR входа
    (`TelegramUser.qr_dir`): из него ядро отдаёт её браузеру дашборда (panel.serve_dirs, блок image с file)."""
    manifest = copy.deepcopy(MANIFEST)
    if serve_dirs:
        manifest["panel"]["serve_dirs"] = list(serve_dirs)
    return manifest
