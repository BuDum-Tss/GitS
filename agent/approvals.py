"""Подтверждения опасных действий.

Когда агент-помощник хочет выполнить команду, записать файл или сходить в сеть,
он просит разрешения: ГЛаДОС произносит вопрос, а ответ «да»/«нет» из чата
(или кнопки в дашборде) разрешает вопрос. Нет ответа за TIMEOUT_SECS — отказ.
"""
import asyncio
import time

import events_log
import settings

TIMEOUT_SECS = 180

_pending: list[dict] = []


def has_pending() -> bool:
    return bool(_pending)


def pending_text() -> str:
    return _pending[0]["text"] if _pending else ""


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
    fut = asyncio.get_running_loop().create_future()
    item = {"text": text, "future": fut, "ts": time.time()}
    _pending.append(item)
    events_log.log("approval", "запрос: " + text)
    try:
        await announce(f"{text} Разрешаешь? Скажи «да» или «нет».")
        return await asyncio.wait_for(fut, TIMEOUT_SECS)
    except asyncio.TimeoutError:
        return False
    finally:
        if item in _pending:
            _pending.remove(item)


def resolve(allowed: bool) -> bool:
    """Ответить на самый старый открытый вопрос. False, если вопросов нет."""
    if not _pending:
        return False
    fut = _pending[0]["future"]
    events_log.log("approval", "ответ: " + ("разрешено" if allowed else "отказано"))
    if not fut.done():
        fut.set_result(allowed)
    return True
