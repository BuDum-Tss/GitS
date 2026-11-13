"""Откуда пришло действие: от просьбы пользователя или от самоимпульса агента (ТЗ 30, ТЗ 32).

Размышление (`Intent do`) отдаёт текст планировщику; планировщик выбирает инструмент, а тот не знает, кто его просит. Признак
живёт в контексте задачи (`ContextVar`) и доходит до инструмента через все `await` и `create_task`. Нужен там, где причина
действия меняет правила: вход в среду модуля по самоимпульсу идёт с обычным разрешением (`workspace.switch`).
"""
from contextlib import contextmanager
from contextvars import ContextVar

USER, SELF = "user", "self"
_origin: ContextVar[str] = ContextVar("impulse_origin", default=USER)


def current() -> str:
    return _origin.get()


@contextmanager
def as_self():
    """Всё, что вызвано внутри блока, считается действием по самоимпульсу."""
    token = _origin.set(SELF)
    try:
        yield
    finally:
        _origin.reset(token)
