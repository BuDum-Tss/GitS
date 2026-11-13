"""Доступна ли сейчас модель единого агента (pi).

Лимит подписки pi приходит ошибкой хода без времени сброса, поэтому после такой ошибки агент отдыхает `PI_COOLDOWN_S`: рабочий
цикл не стучится в закрытую дверь каждые две минуты. Состояние живёт в памяти процесса: после перезапуска ядра первая же
ошибка выставит его снова.
"""
import re
import time

PI_COOLDOWN_S = 15 * 60.0
_LIMIT = re.compile(r"usage limit|rate limit|quota|too many requests|429|limit reached|лимит", re.I)
_pi_until = 0.0


def note_pi_error(text: str | None, now: float | None = None) -> bool:
    """Ошибка хода. True, если это лимит подписки (тогда модель считается закрытой на время отдыха)."""
    global _pi_until
    if not text or not _LIMIT.search(text):
        return False
    _pi_until = (now if now is not None else time.time()) + PI_COOLDOWN_S
    return True


def blocked(now: float | None = None) -> bool:
    return (now if now is not None else time.time()) < _pi_until


def note() -> str:
    """Строка для контекста ответа и для отказа в тяжёлой задаче; пустая, когда модель доступна."""
    return "исчерпан лимит подписки, нужно подождать" if blocked() else ""


def reset() -> None:
    global _pi_until
    _pi_until = 0.0
