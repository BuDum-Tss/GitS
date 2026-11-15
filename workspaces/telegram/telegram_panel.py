"""Панель «Telegram» в дашборде: вход в аккаунт, список всех диалогов с режимами и открытый чат (ТЗ 15, блоки panel.set).

Панель собирается из готовых блоков (`text`, `buttons`, `table`, `input`, `image`, `progress`): отдельного блока «чат» нет.
Сообщения открытого чата это таблица (у строки свои кнопки «Ответить» и «Удалить»), поле «написать» это блок `input`. Так не
нужен ещё один тип блока со своим экранированием, а действия строк уже есть у таблицы.

Состояние панели (какой чат открыт, на какое сообщение отвечаем, сколько сообщений показано, поиск) живёт здесь, в окружении, и
не хранится нигде, кроме памяти процесса. Историю и список диалогов читает Telethon по запросу и держит в коротких кэшах, чтобы
панель не дёргала Telegram на каждое обновление; сами обновления панели ограничивает `PanelClient.set` (не чаще
`PANEL_SET_MIN_INTERVAL_S`). Любое действие возвращает `{ok, message}` и добавляется в `ACTIONS` и в `manifest.panel.actions`:
структура расширяема (закреп, пересылка, вложения, выход из групп добавятся новыми id, ничего не ломая).

Секреты: пароль двух шагов приходит действием `login_password` в поле `value` и сразу уходит в автомат входа. Здесь он не
сохраняется, не попадает ни в сообщения, ни в ошибки, ни в блоки панели.
"""
import asyncio
import json
import logging
import time

from common import CHAT_MODES, MODE_IGNORE, MODE_READ, MODE_REPLY, MODE_WORDS, scrub
from gits_sdk.protocol import PANEL_CELL_MAX, PANEL_SET_MAX_BYTES, PANEL_ROWS_MAX
from tg_qr_login import DONE, ERROR, PASSWORD, WAITING, is_network_error

logger = logging.getLogger("tg_panel")

# все действия панели (кроме них в manifest.panel.actions ничего нет)
ACTIONS = ("login_start", "login_cancel", "login_password", "logout",
           "open_chat", "close_chat", "set_mode", "send_message", "reply_to", "cancel_reply", "history_more",
           "mark_read", "delete_message", "refresh", "more_chats", "filter_chats")

DIALOGS_TTL_S = 20.0             # список диалогов обновляется у Telegram не чаще
HISTORY_TTL_S = 6.0              # история открытого чата
LOGOUT_ARM_S = 10.0              # «выйти» срабатывает со второго нажатия в течение этого времени
CHATS_PAGE = 50                  # сколько чатов показывать за раз (кнопка «Показать ещё»)
HISTORY_PAGE, HISTORY_MAX = 50, 200
TEXT_CLIP = 300                  # длина текста сообщения в строке таблицы
BUDGET_BYTES = PANEL_SET_MAX_BYTES - 6000     # запас под остальные блоки

MODE_HINT = {MODE_IGNORE: "событий из этого чата в ядро нет, содержимое она не читает",
             MODE_READ: "она читает (для внимания и памяти), но писать от своего имени не может",
             MODE_REPLY: "обычный режим: читает и отвечает"}


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _stamp(ts: float, with_date: bool = True) -> str:
    return time.strftime("%d.%m %H:%M" if with_date else "%H:%M", time.localtime(ts)) if ts else ""


def _size(blocks: list) -> int:
    return len(json.dumps({"blocks": blocks}, ensure_ascii=False).encode("utf-8"))


def fit(blocks: list, from_front: bool) -> list:
    """Уложить блоки в предел panel.set: из самой большой таблицы убираются строки (с начала для истории, с конца для списка)."""
    while _size(blocks) > BUDGET_BYTES:
        tables = [b for b in blocks if b.get("type") == "table" and len(b.get("rows") or []) > 1]
        if not tables:
            break
        rows = max(tables, key=lambda b: len(json.dumps(b, ensure_ascii=False)))["rows"]
        rows.pop(0 if from_front else -1)
    return blocks


def _chat_arg(args: dict) -> int:
    chat = args.get("chat")
    if isinstance(chat, bool) or not isinstance(chat, int):
        raise ValueError("нужен чат (число)")
    return chat


class TelegramPanel:
    def __init__(self, module):
        """module - TelegramModule (нужны его user, channel, client)."""
        self.module = module
        self.user = module.user
        self.chat: int | None = None                # открытый чат
        self.reply_to: int | None = None            # на какое сообщение отвечаем
        self.history_limit = HISTORY_PAGE
        self.chat_limit = CHATS_PAGE
        self.filter = ""
        self._logout_until = 0.0
        self._dialogs: list[dict] = []
        self._dialogs_at, self._dialogs_err = 0.0, ""
        self._history: list[dict] = []
        self._history_key: tuple | None = None
        self._history_at, self._history_err = 0.0, ""
        self.wake = asyncio.Event()                 # действие изменило панель: обновить сразу, не ждать цикла

    # ── кэши ─────────────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        self._dialogs_at = self._history_at = 0.0
        self.wake.set()

    async def _load_dialogs(self) -> None:
        if time.monotonic() - self._dialogs_at < DIALOGS_TTL_S:
            return
        self._dialogs_at = time.monotonic()
        try:
            self._dialogs = await self.user.dialogs_view(limit=PANEL_ROWS_MAX)
            self._dialogs_err = ""
        except Exception as e:
            self._dialogs_err = self.explain(e)

    async def _load_history(self) -> None:
        key = (self.chat, self.history_limit)
        if key == self._history_key and time.monotonic() - self._history_at < HISTORY_TTL_S:
            return
        self._history_at = time.monotonic()
        if key != self._history_key:
            self._history = []
        self._history_key = key
        try:
            self._history = await self.user.history(self.chat, self.history_limit)
            self._history_err = ""
        except Exception as e:
            self._history_err = self.explain(e)

    def explain(self, e: BaseException) -> str:
        """Понятный текст ошибки для панели (без секретов и без сырых трассировок)."""
        from telegram_user import ChatModeError, NotAuthorized
        if isinstance(e, (ChatModeError, NotAuthorized)):
            return str(e)
        if is_network_error(e):
            return "нет сети: не удалось связаться с Telegram"
        if isinstance(e, (ValueError, RuntimeError)):
            return scrub(str(e), self.user._api_hash)[:200]
        return f"Telegram-аккаунт: {type(e).__name__}"

    def _dialog(self, chat_id) -> dict | None:
        return next((d for d in self._dialogs if d["id"] == chat_id), None)

    # ── блоки ────────────────────────────────────────────────────────────────

    async def blocks(self) -> list[dict]:
        out = self.account_blocks()
        user = self.user
        if user.state_word() in ("подключён", "подключается"):
            if self.chat is None:
                await self._load_dialogs()
                out += self.list_blocks()
                fit(out, from_front=False)
            else:
                await self._load_dialogs()
                await self._load_history()
                out += self.chat_blocks()
                fit(out, from_front=True)
        out.append(self.channel_block())
        return out

    def channel_block(self) -> dict:
        m = self.module
        last = m.channel.last_post_ts()
        return {"type": "text", "title": "Канал и бот-витрина", "collapsed": True,
                "text": f"Канал: {m._channel_word()}. Последний пост: {_stamp(last) or 'ещё не было'}."}

    def account_blocks(self) -> list[dict]:
        u = self.user
        word = u.state_word()
        login = u._login.view() if u._login is not None else {"state": "idle", "error": "", "who": "", "qr_file": "",
                                                              "expires_in": 0}
        state = login["state"]
        out: list[dict] = []
        if state == WAITING:
            out.append({"type": "text", "title": "Аккаунт",
                        "text": "Ждёт скана. Telegram на телефоне: Настройки - Устройства - Подключить устройство, наведи "
                                "камеру на код и подтверди вход на телефоне. Код меняется сам."})
            if login["qr_file"]:
                out.append({"type": "image", "file": login["qr_file"], "alt": "QR-код для входа в Telegram"})
                left = login["expires_in"]
                out.append({"type": "progress", "label": f"Код действует ещё {left} с", "value": max(0.0, min(1.0, left / 30))})
            out.append({"type": "buttons", "items": [{"label": "Отмена", "action": "login_cancel"}]})
            return out
        if state == PASSWORD:
            note = login["error"] or "На аккаунте включён пароль двух шагов."
            out.append({"type": "text", "title": "Аккаунт", "text": note + " Введи его: он уйдёт прямо в окружение и нигде не сохраняется."})
            out.append({"type": "input", "label": "Пароль двух шагов", "secret": True, "submit": "Войти",
                        "action": "login_password"})
            out.append({"type": "buttons", "items": [{"label": "Отмена", "action": "login_cancel"}]})
            return out
        start = {"label": "Войти по QR", "action": "login_start", "style": "primary"}
        if word == "подключён":
            who = u.me_text() or "аккаунт"
            armed = time.time() < self._logout_until
            out.append({"type": "text", "title": "Аккаунт", "text": f"Вошла как {who}."})
            out.append({"type": "buttons", "items": [
                {"label": "Точно выйти? Нажми ещё раз" if armed else "Выйти из аккаунта", "action": "logout", "style": "danger"}]})
        elif word == "подключается":
            out.append({"type": "text", "title": "Аккаунт", "text": "Вход выполнен, подключается к Telegram…"})
        elif word == "нет ключей":
            out.append({"type": "text", "title": "Аккаунт",
                        "text": "Не настроено: нет TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (my.telegram.org, API development tools)."})
        elif word == "нет telethon":
            out.append({"type": "text", "title": "Аккаунт", "text": "Не установлена библиотека telethon (pip install telethon)."})
        elif word == "нет сети":
            out.append({"type": "text", "title": "Аккаунт",
                        "text": "Нет сети: Telegram недоступен. Сессия на месте, аккаунт подключится сам, когда сеть вернётся."})
        elif word == "ошибка связи":
            out.append({"type": "text", "title": "Аккаунт", "text": f"Ошибка связи: {u.state()[1]}"})
        else:                                                             # нет сессии
            text = "Не вошла в аккаунт."
            if state == ERROR:
                text += f" Прошлая попытка: {login['error']}."
            elif state == DONE and login["who"]:
                text = f"Вход выполнен: {login['who']}, подключаюсь…"
            out.append({"type": "text", "title": "Аккаунт", "text": text})
            out.append({"type": "buttons", "items": [start]})
        return out

    def _row_actions(self, d: dict) -> list[dict]:
        acts = [{"id": "open_chat", "label": "Открыть", "args": {"chat": d["id"]}}]
        if not d.get("owner"):
            acts += [{"id": "set_mode", "label": "→ " + MODE_WORDS[m], "args": {"chat": d["id"], "mode": m}}
                     for m in CHAT_MODES if m != d["mode"]]
        return acts

    def list_blocks(self) -> list[dict]:
        from telegram_user import KIND_WORDS
        out: list[dict] = []
        if self._dialogs_err and not self._dialogs:
            return [{"type": "text", "title": "Чаты", "text": "Список чатов не получен: " + self._dialogs_err}]
        needle = self.filter.lower()
        rows_all = [d for d in self._dialogs if not needle or needle in d["name"].lower()]
        shown = rows_all[:self.chat_limit]
        default = self.user.default_mode()
        out.append({"type": "text", "title": "Чаты аккаунта",
                    "text": f"Режимы: «игнор» - событий в ядро нет; «чтение» - читает, но писать не может; «ответы» - как обычно. "
                            f"Новый чат: «{MODE_WORDS[default]}» (настройка default_chat_mode). Личка владельца всегда «ответы»."
                            + (f" Ошибка обновления: {self._dialogs_err}" if self._dialogs_err else "")})
        out.append({"type": "input", "label": "Поиск чата", "placeholder": "часть названия", "submit": "Найти",
                    "action": "filter_chats"})
        if self.filter:
            out.append({"type": "buttons", "items": [{"label": f"Сбросить поиск «{_clip(self.filter, 30)}»",
                                                      "action": "filter_chats", "args": {"clear": True}}]})
        rows = []
        for d in shown:
            mode = MODE_WORDS[d["mode"]] + ("" if d["explicit"] or d.get("owner") else " (по умолчанию)")
            last = (_stamp(d["ts"]) + " " if d["ts"] else "") + _clip(d["last"], 80)
            rows.append({"cells": [_clip(d["name"], 80), KIND_WORDS[d["kind"]], d["unread"], last.strip(), mode],
                         "actions": self._row_actions(d)})
        out.append({"type": "table", "title": f"Чатов: {len(rows_all)}",
                    "columns": ["Чат", "Тип", "Непрочит.", "Последнее", "Режим"], "rows": rows})
        if len(rows_all) > len(shown):
            out.append({"type": "buttons", "items": [{"label": f"Показать ещё (показано {len(shown)} из {len(rows_all)})",
                                                       "action": "more_chats"}]})
        elif not rows_all:
            out.append({"type": "text", "text": "Диалогов нет." if not self.filter else "Ничего не найдено."})
        out.append({"type": "buttons", "items": [{"label": "Обновить", "action": "refresh"}]})
        return out

    def chat_blocks(self) -> list[dict]:
        from telegram_user import KIND_WORDS
        chat = self.chat
        d = self._dialog(chat) or {"id": chat, "name": f"чат {chat}", "kind": "user", "mode": self.user.mode_of(chat),
                                   "owner": False, "read_out": 0, "explicit": False}
        mode = d["mode"]
        out: list[dict] = [{"type": "buttons", "items": [
            {"label": "← К списку", "action": "close_chat"}, {"label": "Обновить", "action": "refresh"},
            {"label": "Отметить прочитанным", "action": "mark_read", "args": {"chat": chat}}]}]
        head = f"Режим: «{MODE_WORDS[mode]}»: {MODE_HINT[mode]}."
        out.append({"type": "text", "title": f"{d['name']} ({KIND_WORDS[d['kind']]})", "text": head if not d.get("owner")
                    else "Личка владельца: режим всегда «ответы»."})
        if not d.get("owner"):
            out.append({"type": "buttons", "items": [
                {"label": MODE_WORDS[m].capitalize(), "action": "set_mode", "args": {"chat": chat, "mode": m},
                 "style": "primary" if m == mode else "default"} for m in CHAT_MODES]})
        if self._history_err:
            out.append({"type": "text", "text": "История не получена: " + self._history_err})
        rows = []
        for m in self._history[-PANEL_ROWS_MAX:]:
            who = ("→ " if m["out"] else "← ") + _clip(m["who"], 40)
            text = (f"↩#{m['reply_to']} " if m.get("reply_to") else "") + _clip(m["text"], TEXT_CLIP) + (" (ред.)" if m["edited"] else "")
            status = ("✓✓" if m["id"] <= d.get("read_out", 0) else "✓") if m["out"] else ""
            acts = [{"id": "reply_to", "label": "Ответить", "args": {"chat": chat, "id": m["id"]}}]
            if m["out"]:
                acts.append({"id": "delete_message", "label": "Удалить", "args": {"chat": chat, "id": m["id"]}})
            rows.append({"cells": [m["id"], _stamp(m["ts"]), who, _clip(text, PANEL_CELL_MAX), status], "actions": acts})
        out.append({"type": "table", "title": f"Сообщения (последние {len(rows)})",
                    "columns": ["№", "Время", "Кто", "Сообщение", ""], "rows": rows})
        if len(self._history) >= self.history_limit and self.history_limit < HISTORY_MAX:
            out.append({"type": "buttons", "items": [{"label": "Ещё (более старые)", "action": "history_more",
                                                       "args": {"chat": chat}}]})
        label = "Написать от лица агента" if mode == MODE_REPLY else "Написать от имени владельца (режим без ответов)"
        if self.reply_to:
            quoted = next((m for m in self._history if m["id"] == self.reply_to), None)
            out.append({"type": "text", "text": f"Ответ на #{self.reply_to}" + (f": «{_clip(quoted['text'], 80)}»" if quoted else "")})
            out.append({"type": "buttons", "items": [{"label": "Отменить ответ", "action": "cancel_reply"}]})
            label += f" (ответом на #{self.reply_to})"
        out.append({"type": "input", "label": label, "placeholder": "текст сообщения", "submit": "Отправить", "multiline": True,
                    "action": "send_message", "args": {"chat": chat}})
        return out

    # ── действия ─────────────────────────────────────────────────────────────

    async def handle(self, action_id: str, args: dict) -> dict:
        """Выполнить действие панели -> {ok, message}. Сбой превращается в понятный ответ, не в исключение: пароль и тексты
        сообщений не должны оказаться в трассировке."""
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

    def _need_account(self) -> None:
        ok, why = self.user.state()
        if not ok:
            raise RuntimeError(why)

    async def act_login_start(self, args: dict) -> str:
        await self.user.start_login()
        return "Вход по QR начат: отсканируй код в Telegram на телефоне."

    async def act_login_cancel(self, args: dict) -> str:
        await self.user.cancel_login()
        return "Вход отменён."

    async def act_login_password(self, args: dict) -> str:
        if not self.user.submit_login_password(args.get("value")):
            raise ValueError("пароль сейчас не нужен")
        return "Пароль передан, проверяю."

    async def act_logout(self, args: dict) -> str:
        if time.time() >= self._logout_until:
            self._logout_until = time.time() + LOGOUT_ARM_S
            return f"Выход из аккаунта: нажми «Точно выйти?» ещё раз в течение {int(LOGOUT_ARM_S)} с."
        self._logout_until = 0.0
        self.chat, self.reply_to = None, None
        self._dialogs, self._history = [], []
        return await self.user.logout()

    async def act_open_chat(self, args: dict) -> str:
        self._need_account()
        self.chat, self.reply_to, self.history_limit = _chat_arg(args), None, HISTORY_PAGE
        self.invalidate()
        return "Чат открыт."

    async def act_close_chat(self, args: dict) -> str:
        self.chat, self.reply_to, self.history_limit = None, None, HISTORY_PAGE
        self.invalidate()
        return "Список чатов."

    async def act_set_mode(self, args: dict) -> str:
        chat, mode = _chat_arg(args), args.get("mode")
        if mode not in CHAT_MODES:
            raise ValueError(f"режим: одно из {list(CHAT_MODES)}")
        d = self._dialog(chat)
        if self.user.is_owner_chat(chat, None) or (d or {}).get("owner"):
            raise ValueError("личка владельца всегда в режиме «ответы»: её режим не меняется")
        if not await asyncio.to_thread(self.user.modes.set, chat, mode):
            raise RuntimeError("режим не сохранён: не записался файл")
        if d is not None:
            d["mode"], d["explicit"] = mode, True
        self._dialogs_at = 0.0
        return f"Режим чата: «{MODE_WORDS[mode]}»."

    async def act_send_message(self, args: dict) -> str:
        self._need_account()
        chat, text = _chat_arg(args), str(args.get("value") or "").strip()
        if not text:
            raise ValueError("пустое сообщение")
        await self.user.send(str(chat), text, reply_to=self.reply_to, by_owner=True)
        self.reply_to = None
        self.invalidate()
        return "Отправлено."

    async def act_reply_to(self, args: dict) -> str:
        message_id = args.get("id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise ValueError("нужен id сообщения")
        self.reply_to = message_id
        return f"Ответ на #{message_id}: напиши текст ниже."

    async def act_cancel_reply(self, args: dict) -> str:
        self.reply_to = None
        return "Ответ отменён."

    async def act_history_more(self, args: dict) -> str:
        self.history_limit = min(HISTORY_MAX, self.history_limit + HISTORY_PAGE)
        self.invalidate()
        return "Загружаю более старые сообщения."

    async def act_mark_read(self, args: dict) -> str:
        self._need_account()
        await self.user.mark_chat_read(_chat_arg(args))
        self.invalidate()
        return "Отмечено прочитанным."

    async def act_delete_message(self, args: dict) -> str:
        self._need_account()
        message_id = args.get("id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            raise ValueError("нужен id сообщения")
        await self.user.delete_own(_chat_arg(args), message_id)
        if self.reply_to == message_id:
            self.reply_to = None
        self.invalidate()
        return "Сообщение удалено."

    async def act_refresh(self, args: dict) -> str:
        self.invalidate()
        return "Обновляю."

    async def act_more_chats(self, args: dict) -> str:
        self.chat_limit += CHATS_PAGE
        return "Показываю больше чатов."

    async def act_filter_chats(self, args: dict) -> str:
        self.filter = "" if args.get("clear") else str(args.get("value") or "").strip()[:80]
        self.chat_limit = CHATS_PAGE
        return f"Поиск: «{self.filter}»." if self.filter else "Поиск сброшен."
