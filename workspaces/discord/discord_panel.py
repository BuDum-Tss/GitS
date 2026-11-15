"""Панель «Discord» в дашборде: статус бота, ссылка-приглашение, серверы с каналами и ветками и режимы, личные сообщения, открытый
чат (ТЗ 15, блоки panel.set). Образец: workspaces/telegram/telegram_panel.py (те же общие блоки, ничего нового в SDK).

Панель собирается из общих блоков (`text`, `link`, `buttons`, `input`, `cards`, `split`, `table`). Две колонки (`split`): слева
карточки (группа «Личные сообщения» и серверы: аватарка значка, название, число каналов, выпадающий список режима); клик по
карточке раскрывает вложенные (у сервера каналы и ветки или посты форумов, у группы личных собеседники), справа открытый чат:
история (последние 50, «Ещё»), поле «написать от лица агента», «Ответить», «Удалить» своё. Режим канала и ветки: пункт «Как в
сервере (...)» или «Как в канале (...)» снимает свой режим.

Состояние панели (какой чат открыт, какие карточки раскрыты, на какое сообщение отвечаем, сколько сообщений показано, поиск) живёт
здесь, в окружении, в памяти процесса. Серверы, каналы и ветки берутся из кэша бота (без сети), историю читает Discord по запросу
в коротких кэшах; аватарки качаются только видимым карточкам, не более 20 за обновление, с таймаутами (CDN может быть недоступен).
Любая сборка панели ограничена по времени (`PANEL_BUILD_TIMEOUT_S` в discord_module.py), каждый сетевой вызов тоже: в Telegram один
зависший вызов уже замораживал панель. id в аргументах действий строки: числа Discord не помещаются в число JavaScript.
"""
import asyncio
import json
import logging
import time

from common import (CHAT_MODES, MODE_IGNORE, MODE_READ, MODE_REPLY, MODE_WORDS, channel_target, dm_target, parse_target, scrub)
from discord_bot import AVATAR_NEW_MAX, ChatModeError, SendDenied, is_network_error, kind_name
from gits_sdk.protocol import PANEL_CARDS_MAX, PANEL_CELL_MAX, PANEL_ROWS_MAX, PANEL_SET_MAX_BYTES

logger = logging.getLogger("discord.panel")

# все действия панели (кроме них в manifest.panel.actions ничего нет)
ACTIONS = ("refresh", "reconnect", "toggle_guild", "open_chat", "close_chat", "set_mode", "send_message", "reply_to", "cancel_reply",
           "history_more", "delete_message", "filter_guilds")

HISTORY_TTL_S = 6.0              # история открытого чата
HISTORY_TIMEOUT_S = 12.0         # загрузка истории целиком (поиск канала и чтение)
HISTORY_PAGE, HISTORY_MAX = 50, 200
TEXT_CLIP = 300                  # длина текста сообщения в строке таблицы
BUDGET_BYTES = PANEL_SET_MAX_BYTES - 6000     # запас под остальные блоки
INHERIT = "inherit"              # пункт списка канала и ветки «Как в ...»
DM_GROUP = "dm"                  # id раскрывающейся группы «Личные сообщения»
MODE_LABELS = {MODE_IGNORE: "Игнорировать", MODE_READ: "Только читать", MODE_REPLY: "Читать и отвечать"}
MODE_OPTIONS = [{"value": m, "label": MODE_LABELS[m]} for m in CHAT_MODES]
MODE_HINT = {MODE_IGNORE: "событий из этого чата в ядро нет, содержимое она не читает",
             MODE_READ: "она читает (для внимания и памяти), но писать от своего имени не может",
             MODE_REPLY: "обычный режим: читает и отвечает"}


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _stamp(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.localtime(ts)) if ts else ""


def _size(blocks: list) -> int:
    return len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode("utf-8"))


def _walk(blocks):
    """Все блоки, включая вложенные в колонки split."""
    for b in blocks:
        yield b
        if b.get("type") == "split":
            for side in ("left", "right"):
                yield from _walk(b.get(side) or [])


def fit(blocks: list) -> list:
    """Уложить блоки в предел panel.set: из самого большого блока убираются строки: у истории (table) самые старые, у списка
    карточек (cards) последние."""
    while _size(blocks) > BUDGET_BYTES:
        found = [b for b in _walk(blocks) if (b.get("type") == "table" and len(b.get("rows") or []) > 1)
                 or (b.get("type") == "cards" and len(b.get("items") or []) > 1)]
        if not found:
            break
        big = max(found, key=lambda b: len(json.dumps(b, ensure_ascii=False)))
        if big["type"] == "table":
            big["rows"].pop(0)
        else:
            big["items"].pop()
    return blocks


def _id_arg(args: dict, name: str = "id") -> int:
    value = args.get(name)
    text = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not text.isdigit():
        raise ValueError(f"нужен числовой id ({name})")
    return int(text)


def _target_arg(args: dict) -> str:
    target = args.get("target")
    if parse_target(target) is None:
        raise ValueError("нужен адрес чата (channel:<id> или dm:<id>)")
    return target


class DiscordPanel:
    def __init__(self, module):
        """module - DiscordModule (нужны его bot и client)."""
        self.module = module
        self.bot = module.bot
        self.target: str | None = None              # открытый чат
        self.expanded: list[str] = []               # раскрытые карточки (id серверов и DM_GROUP)
        self.reply_to: int | None = None            # на какое сообщение отвечаем
        self.history_limit = HISTORY_PAGE
        self.filter = ""
        self._history: list[dict] = []
        self._history_key: tuple | None = None
        self._history_at, self._history_err = 0.0, ""
        self.wake = asyncio.Event()                 # действие изменило панель: обновить сразу, не ждать цикла

    # ── кэши ─────────────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        self._history_at = 0.0
        self.wake.set()

    async def _load_history(self) -> None:
        key = (self.target, self.history_limit)
        if key == self._history_key and time.monotonic() - self._history_at < HISTORY_TTL_S:
            return
        self._history_at = time.monotonic()
        if key != self._history_key:
            self._history = []
        self._history_key = key
        try:
            self._history = await asyncio.wait_for(self.bot.history(self.target, self.history_limit), HISTORY_TIMEOUT_S)
            self._history_err = ""
        except Exception as e:
            self._history_err = self.explain(e)

    def explain(self, e: BaseException) -> str:
        """Понятный текст ошибки для панели (без секретов и без сырых трассировок)."""
        if isinstance(e, (ChatModeError, SendDenied)):
            return str(e)
        if isinstance(e, asyncio.TimeoutError):
            return "Discord не ответил вовремя: попробуй обновить позже"
        if is_network_error(e):
            return "нет сети: не удалось связаться с Discord"
        if isinstance(e, (ValueError, RuntimeError)):
            return scrub(e, self.bot._token)[:200]
        return f"Discord: {type(e).__name__}"

    # ── блоки ────────────────────────────────────────────────────────────────

    async def blocks(self) -> list[dict]:
        out = self.status_blocks()
        if self.bot.ready():
            if self.target is not None:
                await self._load_history()
            out.append({"type": "split", "left": await self.list_blocks(), "right": self.right_blocks()})
            fit(out)
        return out

    def status_blocks(self) -> list[dict]:
        bot = self.bot
        word = bot.state_word()
        out: list[dict] = []
        if word == "подключён":
            out.append({"type": "text", "title": "Бот", "text": f"Вошла как {bot.me_text()}. Серверов: {bot.guild_count()}."})
            url = bot.invite_url()
            if url:
                out.append({"type": "link", "label": "Пригласить бота на сервер", "url": url})
        elif word == "подключается":
            out.append({"type": "text", "title": "Бот", "text": "Подключается к Discord…"})
        elif word == "не настроен":
            out.append({"type": "text", "title": "Бот",
                        "text": "Не настроено: нет DISCORD_BOT_TOKEN в .env. Создай приложение в Discord Developer Portal "
                                "(discord.com/developers/applications), раздел Bot: Reset Token, токен в .env; там же включи "
                                "Message Content Intent. Потом перезапусти окружение."})
        elif word == "нет discord.py":
            out.append({"type": "text", "title": "Бот", "text": "Не установлена библиотека discord.py (pip install \"discord.py>=2.4\")."})
        else:                                                             # неверный токен, нет intent, нет сети, ошибка связи
            out.append({"type": "text", "title": "Бот", "text": _clip(f"Не подключена ({word}): {bot.state()[1]}", 900)})
            out.append({"type": "buttons", "items": [{"label": "Проверить снова", "action": "reconnect"}]})
        return out

    # ── список (левая колонка) ───────────────────────────────────────────────

    def _select(self, scope: str, ident: int, value: str | None, hint: str, inherit: str | None = None) -> dict:
        options = ([{"value": INHERIT, "label": _clip(inherit, 70)}] if inherit else []) + MODE_OPTIONS
        return {"options": options, "value": value or (INHERIT if inherit else MODE_REPLY),
                "action": "set_mode", "args": {"scope": scope, "id": str(ident)}, "arg": "mode", "hint": hint}

    async def _guild_card(self, g: dict, allm: dict, avatars: list[int]) -> dict:
        gid, bot = g["id"], self.bot
        default = bot.default_mode()
        gmode = allm["guild"].get(str(gid)) or default
        total = sum(1 + len(c["threads"]) for c in g["channels"])
        card: dict = {"title": _clip(g["name"], 80), "subtitle": f"каналов: {len(g['channels'])}" + (
            f", веток и постов: {total - len(g['channels'])}" if total > len(g["channels"]) else ""),
            "select": self._select("guild", gid, gmode, "Режим всего сервера (каналы и ветки без своего режима)"),
            "action": "toggle_guild", "args": {"id": str(gid)}, "expanded": str(gid) in self.expanded}
        if gmode == MODE_IGNORE:
            card["muted"] = True
        name = await bot.avatar_file("g", gid, g.get("icon"), avatars)
        if name:
            card["avatar"] = name
        if str(gid) in self.expanded:
            card["children"] = self._channel_cards(g, allm, gmode)
        return card

    def _channel_cards(self, g: dict, allm: dict, gmode: str) -> list[dict]:
        """Каналы сервера и их ветки (посты форума) вложенными карточками (не глубже одного уровня): ветка идёт сразу за каналом."""
        out = []
        for c in g["channels"]:
            cmode_own = allm["channel"].get(str(c["id"]))
            cmode = cmode_own or gmode
            forum = c["kind"] == "forum"
            card: dict = {"title": _clip(("" if forum else "#") + c["name"], 80),
                          "select": self._select("channel", c["id"], cmode_own, "Режим канала",
                                                 f"Как в сервере ({MODE_WORDS[gmode]})")}
            if forum:
                card["tag"] = "форум"
            else:
                card["action"], card["args"] = "open_chat", {"target": channel_target(c["id"])}
                card["selected"] = self.target == channel_target(c["id"])
            if cmode == MODE_IGNORE:
                card["muted"] = True
            out.append(card)
            for t in c["threads"]:
                tmode_own = allm["thread"].get(str(t["id"]))
                tcard: dict = {"title": _clip("↳ " + t["name"], 80), "tag": ("архив" if t["archived"] else "пост" if forum else "ветка"),
                               "action": "open_chat", "args": {"target": channel_target(t["id"])},
                               "selected": self.target == channel_target(t["id"]),
                               "select": self._select("thread", t["id"], tmode_own, "Режим ветки",
                                                      f"Как в канале ({MODE_WORDS[cmode]})")}
                if (tmode_own or cmode) == MODE_IGNORE:
                    tcard["muted"] = True
                out.append(tcard)
        return out[:PANEL_CARDS_MAX]

    async def _dm_card(self, d: dict, avatars: list[int]) -> dict:
        target = dm_target(d["id"])
        card: dict = {"title": _clip(d["name"], 80), "action": "open_chat", "args": {"target": target},
                      "selected": self.target == target}
        last = max(d["last_in"], d["last_out"])
        if last:
            card["subtitle"] = ("Вы: " if d["last_out"] >= d["last_in"] else "") + f"последнее {_stamp(last)}"
        if d["owner"]:
            card["tag"] = "владелец"
        else:
            card["select"] = self._select("dm", d["id"], d["mode"], "Режим лички")
            if d["mode"] == MODE_IGNORE:
                card["muted"] = True
        name = await self.bot.avatar_file("u", d["id"], d.get("avatar"), avatars)
        if name:
            card["avatar"] = name
        return card

    async def list_blocks(self) -> list[dict]:
        bot = self.bot
        out: list[dict] = [{"type": "text", "title": "Режимы", "collapsed": True,
                            "text": "«Игнорировать»: событий в ядро нет; «Только читать»: читает, но писать не может; «Читать и отвечать»: как "
                                    f"обычно. Режим ветки важнее режима канала, режим канала важнее режима сервера. Новый сервер: «{MODE_WORDS[bot.default_mode()]}» "
                                    "(настройка default_chat_mode). Личка владельца всегда «ответы»."}]
        out.append({"type": "input", "label": "Поиск сервера", "placeholder": "часть названия", "submit": "Найти",
                    "action": "filter_guilds"})
        if self.filter:
            out.append({"type": "buttons", "items": [{"label": f"Сбросить поиск «{_clip(self.filter, 30)}»",
                                                      "action": "filter_guilds", "args": {"clear": True}}]})
        allm, avatars = bot.modes.all(), [AVATAR_NEW_MAX]
        needle = self.filter.lower()
        guilds = [g for g in bot.guilds_view() if not needle or needle in g["name"].lower()]
        cards: list[dict] = []
        dms = bot.dms_view()
        if dms and not needle:
            group: dict = {"title": "Личные сообщения", "tag": str(len(dms)), "action": "toggle_guild", "args": {"id": DM_GROUP},
                           "expanded": DM_GROUP in self.expanded, "icon": "@"}
            if DM_GROUP in self.expanded:
                group["children"] = [await self._dm_card(d, avatars) for d in dms[:PANEL_CARDS_MAX]]
            cards.append(group)
        for g in guilds[:PANEL_CARDS_MAX - len(cards)]:
            cards.append(await self._guild_card(g, allm, avatars))
        if cards:
            out.append({"type": "cards", "title": f"Серверов: {len(guilds)}", "items": cards})
        else:
            out.append({"type": "text", "text": "Бот пока ни на одном сервере: пригласи его по ссылке выше." if not self.filter
                        else "Ничего не найдено."})
        out.append({"type": "buttons", "items": [{"label": "Обновить", "action": "refresh"}]})
        return out

    # ── открытый чат (правая колонка) ────────────────────────────────────────

    def _describe(self, target: str) -> tuple[str, str]:
        """(название для шапки, режим) открытого чата по кэшу бота."""
        kind, ident = parse_target(target)
        bot = self.bot
        mode = bot.mode_of_target(target)
        if kind == "dm":
            row = next((d for d in bot.dms_view() if d["id"] == ident), None)
            return "Личка: " + (row["name"] if row else f"id{ident}"), mode
        ch = bot._client.get_channel(ident) if bot._client is not None else None
        if ch is None:
            return f"канал {ident}", mode
        guild = getattr(ch, "guild", None)
        parent = getattr(ch, "parent", None)
        path = [getattr(guild, "name", "")]
        if parent is not None:
            path.append(("" if kind_name(parent) == "forum" else "#") + str(parent.name))
            path.append(str(ch.name))
        else:
            path.append("#" + str(ch.name))
        return " › ".join(p for p in path if p), mode

    def right_blocks(self) -> list[dict]:
        if self.target is None:
            return [{"type": "text", "text": "Выбери чат слева: откроется переписка. Сервер раскрывается кликом по карточке."}]
        return self.chat_blocks()

    def chat_blocks(self) -> list[dict]:
        target = self.target
        title, mode = self._describe(target)
        kind, ident = parse_target(target)
        arg = {"target": target}
        owner = kind == "dm" and self.bot.is_owner_user(ident)
        card: dict = {"title": _clip(title, 100),
                      "subtitle": ("Личка владельца: режим всегда «ответы»." if owner
                                   else f"Режим: «{MODE_WORDS[mode]}»: {MODE_HINT[mode]}.")}
        out: list[dict] = [{"type": "cards", "items": [card]},
                           {"type": "buttons", "items": [{"label": "← К списку", "action": "close_chat"},
                                                         {"label": "Обновить", "action": "refresh"}]}]
        if self._history_err:
            out.append({"type": "text", "text": "История не получена: " + self._history_err})
        rows = []
        for m in self._history[-PANEL_ROWS_MAX:]:
            who = ("→ " if m["out"] else "← ") + _clip(m["who"], 40)
            quote = f"↩#{m['reply_to']} " if m.get("reply_to") else ""
            text = quote + _clip(m["text"], TEXT_CLIP) + (" (ред.)" if m["edited"] else "")
            acts = [{"id": "reply_to", "label": "Ответить", "args": {"target": target, "id": str(m["id"])}}]
            if m["out"]:
                acts.append({"id": "delete_message", "label": "Удалить", "args": {"target": target, "id": str(m["id"])}})
            rows.append({"cells": [str(m["id"]), _stamp(m["ts"]), who, _clip(text, PANEL_CELL_MAX)], "actions": acts})
        out.append({"type": "table", "title": f"Сообщения (последние {len(rows)})",
                    "columns": ["№", "Время", "Кто", "Сообщение"], "rows": rows})
        if len(self._history) >= self.history_limit and self.history_limit < HISTORY_MAX:
            out.append({"type": "buttons", "items": [{"label": "Ещё (более старые)", "action": "history_more", "args": arg}]})
        label = "Написать от лица агента" if mode == MODE_REPLY else "Написать от имени владельца (режим без ответов)"
        if self.reply_to:
            quoted = next((m for m in self._history if m["id"] == self.reply_to), None)
            out.append({"type": "text", "text": f"Ответ на #{self.reply_to}" + (f": «{_clip(quoted['text'], 80)}»" if quoted else "")})
            out.append({"type": "buttons", "items": [{"label": "Отменить ответ", "action": "cancel_reply"}]})
            label += f" (ответом на #{self.reply_to})"
        out.append({"type": "input", "label": _clip(label, 80), "placeholder": "текст сообщения", "submit": "Отправить",
                    "multiline": True, "action": "send_message", "args": arg})
        return out

    # ── действия ─────────────────────────────────────────────────────────────

    async def handle(self, action_id: str, args: dict) -> dict:
        """Выполнить действие панели -> {ok, message}. Сбой превращается в понятный ответ, не в исключение."""
        fn = getattr(self, "act_" + action_id, None)
        if fn is None:
            return {"ok": False, "message": "такого действия нет"}
        try:
            message = await fn(args)
        except Exception as e:
            logger.info("действие %s: %s", action_id, type(e).__name__)
            return {"ok": False, "message": self.explain(e)}
        self.wake.set()
        return {"ok": True, "message": message or "Готово."}

    def _need_bot(self) -> None:
        ok, why = self.bot.state()
        if not ok:
            raise RuntimeError(why)

    async def act_refresh(self, args: dict) -> str:
        self.invalidate()
        return "Обновляю."

    async def act_reconnect(self, args: dict) -> str:
        self.bot.wake()
        return "Проверяю подключение."

    async def act_toggle_guild(self, args: dict) -> str:
        """Раскрыть или свернуть карточку сервера или группы «Личные сообщения» (состояние живёт здесь, см. docstring модуля)."""
        key = str(args.get("id") or "")
        if key != DM_GROUP and not key.isdigit():
            raise ValueError("нужен id сервера")
        if key in self.expanded:
            self.expanded.remove(key)
            self.invalidate()
            return "Свёрнуто."
        self.expanded.append(key)
        self.invalidate()
        return "Раскрыто."

    async def act_open_chat(self, args: dict) -> str:
        self._need_bot()
        self.target, self.reply_to, self.history_limit = _target_arg(args), None, HISTORY_PAGE
        self.invalidate()
        return "Чат открыт."

    async def act_close_chat(self, args: dict) -> str:
        self.target, self.reply_to, self.history_limit = None, None, HISTORY_PAGE
        self.invalidate()
        return "Список."

    async def act_set_mode(self, args: dict) -> str:
        """Режим сервера, канала, ветки или лички; у канала и ветки mode=inherit снимает свой режим («Как в сервере/канале»)."""
        scope, ident, mode = args.get("scope"), _id_arg(args), args.get("mode")
        if scope not in ("guild", "channel", "thread", "dm"):
            raise ValueError("область режима: guild, channel, thread или dm")
        if mode == INHERIT and scope in ("channel", "thread"):
            mode = None
        elif mode not in CHAT_MODES:
            raise ValueError(f"режим: одно из {list(CHAT_MODES)}" + (" или inherit" if scope in ("channel", "thread") else ""))
        if scope == "dm" and self.bot.is_owner_user(ident, [self.bot.known.name_of(dm_target(ident))]):
            raise ValueError("личка владельца всегда в режиме «ответы»: её режим не меняется")
        if not await asyncio.to_thread(self.bot.modes.set, scope, ident, mode):
            raise RuntimeError("режим не сохранён: не записался файл")
        return f"Режим: «{MODE_WORDS[mode]}»." if mode else "Режим: как выше."

    async def act_send_message(self, args: dict) -> str:
        self._need_bot()
        target, text = _target_arg(args), str(args.get("value") or "").strip()
        if not text:
            raise ValueError("пустое сообщение")
        await self.bot.send(target, text, reply_to=self.reply_to, by_owner=True, target=target)
        self.reply_to = None
        self.invalidate()
        return "Отправлено."

    async def act_reply_to(self, args: dict) -> str:
        self.reply_to = _id_arg(args)
        return f"Ответ на #{self.reply_to}: напиши текст ниже."

    async def act_cancel_reply(self, args: dict) -> str:
        self.reply_to = None
        return "Ответ отменён."

    async def act_history_more(self, args: dict) -> str:
        self.history_limit = min(HISTORY_MAX, self.history_limit + HISTORY_PAGE)
        self.invalidate()
        return "Загружаю более старые сообщения."

    async def act_delete_message(self, args: dict) -> str:
        self._need_bot()
        message_id = _id_arg(args)
        await self.bot.delete_own(_target_arg(args), message_id)
        if self.reply_to == message_id:
            self.reply_to = None
        self.invalidate()
        return "Сообщение удалено."

    async def act_filter_guilds(self, args: dict) -> str:
        self.filter = "" if args.get("clear") else str(args.get("value") or "").strip()[:80]
        return f"Поиск: «{self.filter}»." if self.filter else "Поиск сброшен."
