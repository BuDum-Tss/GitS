"""Манифест модуля Discord (по образцу workspaces/telegram/manifest.py). Описания инструментов читает планировщик."""
import copy

from common import CHAT_MODES, MODE_REPLY
from discord_voice import DEFAULT_ANNOUNCE
from discord_panel import ACTIONS as PANEL_ACTIONS

_GENERIC = ("дискорд", "discord", "сервер", "канал", "ветка", "личка", "написать", "чат")      # общие слова просьб про Discord


def _tool(name, description, params, *, risk="auto", keywords=(), informative=False,
          danger_check=False, timeout_s=30, sends=False):
    tool = {"name": name, "description": description, "params": params, "risk": risk, "availability": "always",
            "timeout_s": timeout_s, "keywords": list(dict.fromkeys([*keywords, *_GENERIC]))}
    if informative:
        tool["informative"] = True
    if danger_check:
        tool["danger_check"] = True
    if sends:
        tool["sends"] = True              # отправляет текст людям: ядро пишет её исходящее событие module_out
    return tool


TOOLS = [
    _tool("message", "Отправить сообщение в Discord от имени бота: владельцу в личку (to=owner), в канал, ветку или пост форума "
                     "(название, #название, «Сервер / канал», id) или человеку, который уже писал боту (@имя). В незнакомый "
                     "канал спрашивает разрешение. Длинный текст режется на части по 2000 символов.",
          {"to": {"type": "string", "required": False,
                  "description": "owner, channel (последний канал, откуда ей писали) или канал/человек: название, #название, @имя, "
                                 "«Сервер / канал», channel:<id>, dm:<id>"},
           "text": {"type": "string", "required": False, "description": "текст сообщения (до 2000 символов в одном сообщении)"},
           "reply_to": {"type": "string", "required": False, "description": "id сообщения, на которое ответить"}},
          informative=True, danger_check=True, timeout_s=60,
          sends=True, keywords=("напиши", "отправь", "ответь", "сообщение", "ответ")),
    _tool("react", "Поставить (или снять, remove=true) реакцию-эмодзи на сообщение в Discord: когда хватает короткого отклика "
                   "(согласие, спасибо, смешно), без отдельного ответа. Без message_id реагирует на последнее входящее "
                   "сообщение канала, откуда ей писали; id видны в inbox (#id). В чат «только чтение» нельзя.",
          {"emoji": {"type": "string", "description": "юникод-эмодзи («👍») или кастомное <:имя:id> / имя:id"},
           "message_id": {"type": "string", "required": False, "description": "id сообщения (число); не задан: последнее входящее"},
           "to": {"type": "string", "required": False,
                  "description": "канал, ветка или человек, как у message (по умолчанию channel: последний канал, откуда ей писали)"},
           "remove": {"type": "boolean", "required": False, "description": "true: снять свою реакцию (по умолчанию поставить)"}},
          informative=True, danger_check=True, keywords=("реакция", "эмодзи", "лайк", "поставь")),
    _tool("inbox", "Читать Discord бота: без параметров серверы и каналы с режимами; channel - последние сообщения канала, ветки "
                   "или лички (название, #название, «Сервер / канал», id) и сколько она сама в нём писала за час и сутки; "
                   "unread=true - что писали недавно. У сообщений указан id (#id).",
          {"channel": {"type": "string", "required": False, "description": "название канала, «Сервер / канал», channel:<id>, dm:<id> или @имя"},
           "unread": {"type": "boolean", "required": False, "description": "true: что писали боту недавно"},
           "limit": {"type": "integer", "required": False, "description": "сколько записей, по умолчанию 15"}},
          informative=True, timeout_s=60, keywords=("прочитай", "переписка", "сообщения", "чаты", "новые", "входящие")),
    _tool("voice_join", "Зайти в голосовой канал Discord и слушать его: речь участников идёт к ней фразами, отвечает она инструментом "
                        "voice.say. Без channel заходит туда, где сидит владелец. В незнакомый канал спрашивает разрешение; "
                        "режим «игнор» не пускает, «чтение» слушает молча.",
          {"channel": {"type": "string", "required": False,
                       "description": "«Сервер / Канал», #название, название или id голосового канала (по умолчанию где сидит владелец)"}},
          informative=True, danger_check=True, timeout_s=90, keywords=("голос", "голосовой", "зайди", "войс", "созвон", "подключись")),
    _tool("voice_leave", "Выйти из голосового канала Discord (из текущего; если их несколько, укажи channel).",
          {"channel": {"type": "string", "required": False, "description": "сервер или канал, откуда выйти (если она сидит в нескольких)"}},
          informative=True, keywords=("голос", "голосовой", "выйди", "войс", "отключись")),
]

MANIFEST = {
    "id": "discord",
    "role": "workspace",
    "ownership": "owned",
    "title": "Discord",
    "description": "Discord: бот на серверах (токен в .env; каналы, ветки, посты форумов и личка; режимы сервера, канала и ветки: игнор, "
                   "чтение, ответы), события discord.dm и discord.message (упоминание обязательно к ответу в режиме «ответы»). "
                   "Панель: серверы, переписка, ссылка-приглашение.",
    "version": "1.0.0",
    "capabilities": ["chat.channel"],
    "subscribe": ["settings", "presence", "module", "character"],
    "shared_settings": ["quiet_until", "dnd"],
    "settings_schema": {
        "owner": {"type": "string", "title": "Владелец в Discord: числовой id или имя пользователя (его личка всегда в режиме «ответы»)",
                  "default": ""},
        "names": {"type": "string", "title": "Как её зовут в каналах: имена через запятую (к ней обратились, если имя есть в "
                                             "тексте; упоминание бота и ответ на его сообщение считаются всегда)", "default": ""},
        "default_chat_mode": {"type": "string", "enum": list(CHAT_MODES), "default": MODE_REPLY,
                              "title": "Режим нового сервера, канала и лички: ignore (событий нет), read (читает, не пишет), reply "
                                       "(читает и отвечает); у каждого меняется в панели"},
        "dm_flood_per_10min": {"type": "integer", "title": "Лавина в личке: если один человек написал за 10 минут больше "
                               "стольких сообщений, следующие идут в сводку (0: выключено; ничего не блокируется)",
                               "default": 20, "min": 0, "max": 1000},
        "voice_announce": {"type": "boolean", "default": True,
                           "title": "Голос: при входе в голосовой канал писать в его текстовый чат объявление, что канал слушается"},
        "voice_announce_text": {"type": "string", "default": DEFAULT_ANNOUNCE, "title": "Голос: текст объявления при входе (без пингов)"},
        "voice_alone_timeout_s": {"type": "integer", "default": 120, "min": 0, "max": 3600,
                                  "title": "Голос: через сколько секунд выйти, если в канале остался только бот (0: не выходить)"},
        "voice_autojoin": {"type": "string", "default": "",
                           "title": "Голос: канал («Сервер / Канал» или id), куда заходить самой, когда в нём есть люди (пусто: нет)"},
        "dialog_window_mins": {"type": "integer", "title": "Продолжение диалога: сколько минут после её ответа собеседник "
                               "считается говорящим с ней без упоминания (0: выключено)", "default": 15, "min": 0, "max": 240},
    },
    "tools": TOOLS,
    "emits": [
        {"kind": "discord.dm", "description": "Личное сообщение боту (чужому отвечает она сама, лавина идёт в сводку)", "default_urgency": 3},
        {"kind": "discord.message", "description": "Сообщение в канале, ветке или посте форума (упоминание или ответ ей: обязательный разговор, правило R6)", "default_urgency": 1},
    ],
    "state": True,
    # пространство в дашборде (TZ 15): статус бота, приглашение, серверы с режимами, открытый чат; действия владельца (не инструменты агента)
    "panel": {"title": "Discord", "icon": "message-circle", "order": 61, "actions": list(PANEL_ACTIONS)},
}


def build_manifest(serve_dirs: list[str] | None = None) -> dict:
    """Свежая копия манифеста. `serve_dirs` - каталог аватарок (`DiscordBot.avatar_dir`): из него ядро отдаёт их браузеру
    дашборда (panel.serve_dirs, поле avatar карточек)."""
    manifest = copy.deepcopy(MANIFEST)
    if serve_dirs:
        manifest["panel"]["serve_dirs"] = list(serve_dirs)
    return manifest
