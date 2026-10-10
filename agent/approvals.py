"""Вопросы пользователю и подтверждения опасных действий.

Общая функция `ask(kind, question, options, timeout_s, source)` (ТЗ 02, CORE-30) задаёт один вопрос и ждёт ответа:
  approval - да или нет (ответ True/False), choice - один из вариантов (строка), text - свободный ответ (строка).
По тайм-ауту approval даёт False, choice и text дают None, ответ приходит с пометкой by="timeout".

Вопросы стоят в очереди: одновременно задаётся один, следующий - после ответа или тайм-аута. Ответить можно словами
в разговоре (`try_answer`: для approval «да»/«нет», для choice совпадение с вариантом по началу слова, для text следующая
реплика целиком) или кнопками в дашборде (`resolve`, `answer`).

`request(text, announce, danger)` остаётся прежним входом для опасных действий агента-помощника, маршрутизатора и
Telegram: это обёртка над `ask(kind="approval")`. Нет ответа за TIMEOUT_SECS - отказ.
"""
import asyncio
import logging
import os
import re
import secrets
import time
from collections import deque

import events_log
import settings

logger = logging.getLogger("approvals")

TIMEOUT_SECS = 180            # request(): сколько ждём ответа на разрешение действия
DEFAULT_TIMEOUT_S = 300       # ask(): по умолчанию (ТЗ 01, 7.3)
MAX_TIMEOUT_S = 86400
KINDS = ("approval", "choice", "text")

YES = ("да", "ага", "разрешаю", "давай", "ок", "окей", "валяй", "можно", "yes", "конечно")
NO = ("нет", "не надо", "не разрешаю", "запрещаю", "отмена", "стоп", "no", "не нужно")

_active: dict | None = None            # вопрос, который задан сейчас
_waiters: deque = deque()              # будущие в очереди на право задать вопрос
_busy = False
_tasks: set = set()                    # фоновые переспрашивания (ссылки, чтобы задачи не собрались сборщиком)


# ── очередь: один вопрос за раз ─────────────────────────────────────────────

async def _enter():
    global _busy
    if not _busy:
        _busy = True
        return
    fut = asyncio.get_running_loop().create_future()
    _waiters.append(fut)
    try:
        await fut                      # право задать вопрос передаёт _leave (_busy остаётся True)
    except asyncio.CancelledError:
        if fut.done() and not fut.cancelled():
            _leave()                   # право успели передать нам, а нас отменили: передаём дальше
        elif fut in _waiters:
            _waiters.remove(fut)
        raise


def _leave():
    global _busy
    while _waiters:
        fut = _waiters.popleft()
        if not fut.done():
            fut.set_result(None)
            return
    _busy = False


# ── состояние для дашборда и разговора ───────────────────────────────────────

def has_pending() -> bool:
    return _active is not None


def pending_text() -> str:
    return _active["text"] if _active else ""


def pending_kind() -> str | None:
    return _active["kind"] if _active else None


def pending_options() -> list[str]:
    return list(_active["options"]) if _active else []


def pending_info() -> dict | None:
    """Активный вопрос для дашборда: {id, kind, text, options, source, queued} или None."""
    if _active is None:
        return None
    return {"id": _active["id"], "kind": _active["kind"], "text": _active["text"], "options": list(_active["options"]),
            "source": _active["source"], "queued": len(_waiters)}


def question_fact(kind: str, question: str, options: list[str] | None, source: str) -> str:
    """Факт о вопросе для модели: формулировку вслух строит она в своём характере, а не код (CORE-31)."""
    who = f"Источник вопроса: {source}. " if source and source != "core" else ""
    how = {"approval": "Ответ нужен «да» или «нет».",
           "choice": "Варианты ответа: " + ", ".join(options or []) + ".",
           "text": "Нужен свободный ответ."}[kind]
    return f"Нужен ответ пользователя на вопрос. {who}Вопрос: {question} {how}"


# ── вопрос ──────────────────────────────────────────────────────────────────

async def ask(kind: str, question: str, options: list[str] | None = None, timeout_s: float = DEFAULT_TIMEOUT_S,
              source: str = "core", announce=None, context: dict | None = None):
    """Задать вопрос пользователю и дождаться ответа. Возвращает (answer, by): by - "user" или "timeout".

    announce(fact) - как сообщить вопрос пользователю (озвучить, показать); получает факт о вопросе, формулировку
    строит вызывающий. Без announce вопрос виден только в дашборде. Отмена вызывающей задачи снимает вопрос."""
    global _active
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид вопроса: {kind}")
    if kind == "choice" and not options:
        raise ValueError("для choice нужен непустой список вариантов")
    try:
        timeout = max(0.01, min(float(timeout_s), MAX_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = float(DEFAULT_TIMEOUT_S)
    await _enter()
    try:
        item = {"id": "q-" + secrets.token_hex(3), "kind": kind, "text": question, "options": list(options or []),
                "source": source, "ts": time.time(), "context": context or {}, "announce": announce,
                "future": asyncio.get_running_loop().create_future(), "retried": False}
        _active = item
        events_log.log("approval", f"запрос ({kind}, {source}): {question}")
        try:
            if announce is not None:
                try:
                    await announce(question_fact(kind, question, item["options"], source))
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Не удалось сообщить вопрос пользователю: {e}")
            try:
                answer, by = await asyncio.wait_for(item["future"], timeout)
            except asyncio.TimeoutError:
                answer, by = (False if kind == "approval" else None), "timeout"
                events_log.log("approval", "ответ: нет ответа за время ожидания")
        finally:
            _active = None
        return answer, by
    finally:
        _leave()


async def request(text: str, announce, danger: str | None = None) -> bool:
    """Спросить разрешение. announce(text) - как озвучить вопрос пользователю.

    При включённом auto_accept обычные действия разрешаются сразу (запись в протокол остаётся).
    danger - причина из safety.py: такое действие спрашивается ВСЕГДА, даже с авто-разрешением."""
    auto = settings.load()["auto_accept"]
    if auto and not danger:
        events_log.log("approval", "авто-разрешено: " + text)
        return True
    if danger:
        text = f"{text} Внимание: {danger}." + (" Спрашиваю, даже несмотря на авто-разрешение." if auto else "")

    async def say(_fact: str):
        await announce(f"{text} Разрешаешь? Скажи «да» или «нет».")
    answer, _by = await ask("approval", text, timeout_s=TIMEOUT_SECS, source="core", announce=say)
    return bool(answer)


# ── ответ ───────────────────────────────────────────────────────────────────

def _finish(answer, label: str) -> bool:
    if _active is None:
        return False
    fut = _active["future"]
    events_log.log("approval", "ответ: " + label)
    if not fut.done():
        fut.set_result((answer, "user"))
    return True


def resolve(allowed: bool) -> bool:
    """Ответить «да»/«нет» на открытый вопрос-разрешение. False, если вопроса нет или он не про да/нет."""
    if _active is None or _active["kind"] != "approval":
        return False
    return _finish(bool(allowed), "разрешено" if allowed else "отказано")


def answer(value) -> bool:
    """Ответ из дашборда: bool для approval, вариант для choice, строка для text. False, если не подходит."""
    if _active is None:
        return False
    kind = _active["kind"]
    if kind == "approval":
        return isinstance(value, bool) and resolve(value)
    if kind == "choice":
        return isinstance(value, str) and value in _active["options"] and _finish(value, value)
    text = str(value or "").strip()
    return bool(text) and _finish(text, text[:200])


_WORD = re.compile(r"[a-zа-яё0-9]+", re.I)


def _word_match(said: str, option: str) -> bool:
    if said == option:
        return True
    if len(said) >= 3 and option.startswith(said):
        return True                                        # начало слова: «утв» -> «утвердить»
    n = min(len(said), len(option))
    return n >= 4 and len(os.path.commonprefix([said, option])) >= n - 2     # окончания: «продолжай» ~ «продолжать»


def match_options(text: str, options: list[str]) -> list[str]:
    """Варианты, с которыми совпадает реплика (без регистра, по началу слова). Пусто - ни с одним."""
    said = _WORD.findall(text.lower())
    if not said:
        return []
    found = []
    for opt in options:
        words = _WORD.findall(opt.lower())
        if text.lower().strip(" .!?") == opt.lower() or any(_word_match(s, w) for s in said for w in words):
            found.append(opt)
    return found


async def try_answer(text: str) -> bool:
    """Принять реплику пользователя как ответ на открытый вопрос. True, если реплика потрачена на ответ (или на
    переспрашивание); False, если это обычная реплика и вопрос остаётся ждать."""
    if _active is None:
        return False
    kind = _active["kind"]
    low = text.lower().strip(" .!")
    if kind == "approval":
        if len(low.split()) > 4:
            return False
        if any(low == w or low.startswith(w + " ") for w in YES):
            return resolve(True)
        if any(low == w or low.startswith(w + " ") for w in NO):
            return resolve(False)
        return False
    if kind == "text":
        return bool(text.strip()) and _finish(text.strip(), text.strip()[:200])
    found = match_options(text, _active["options"])
    if len(found) == 1:
        return _finish(found[0], found[0])
    if len(found) > 1 and not _active["retried"]:
        _active["retried"] = True                           # неоднозначно: переспросить один раз
        announce = _active["announce"]
        if announce is not None:
            fact = (f"Ответ пользователя «{text[:80]}» подходит сразу к нескольким вариантам ({', '.join(found)}). "
                    + question_fact(kind, _active["text"], _active["options"], _active["source"]))
            task = asyncio.get_running_loop().create_task(_reask(announce, fact))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
        return True
    return False


async def _reask(announce, fact: str):
    try:
        await announce(fact)
    except Exception as e:
        logger.warning(f"Не удалось переспросить: {e}")


def reset():
    """Сбросить состояние (для тестов): очередь и открытый вопрос."""
    global _active, _busy
    _active = None
    _busy = False
    _waiters.clear()
