"""Доступна ли сейчас модель единого агента: один вопрос для обеих сред исполнения (`agent_runtime`).

На Claude ответ даёт `claude_quota` (лимит подписки с временем сброса из текста ошибки). На pi лимит подписки приходит ошибкой
хода без времени сброса, поэтому после такой ошибки агент отдыхает `PI_COOLDOWN_S`: рабочий цикл не стучится в закрытую дверь
каждые две минуты. Состояние pi живёт в памяти процесса: после перезапуска ядра первая же ошибка выставит его снова.
"""
import re
import time

PI_COOLDOWN_S = 15 * 60.0
_LIMIT = re.compile(r"usage limit|rate limit|quota|too many requests|\b429\b|limit reached|лимит", re.I)
_pi_until = 0.0


def is_pi() -> bool:
    import settings
    return settings.load().get("agent_runtime") == "pi"


def note_pi_error(text: str | None, now: float | None = None) -> bool:
    """Ошибка хода на pi. True, если это лимит подписки (тогда среда считается закрытой на время отдыха)."""
    global _pi_until
    if not text or not _LIMIT.search(text):
        return False
    _pi_until = (now if now is not None else time.time()) + PI_COOLDOWN_S
    return True


def blocked(now: float | None = None) -> bool:
    if is_pi():
        return (now if now is not None else time.time()) < _pi_until
    import claude_quota
    return claude_quota.blocked()


def note() -> str:
    """Строка для контекста ответа и для отказа в тяжёлой задаче; пустая, когда модель доступна."""
    if not blocked():
        return ""
    if is_pi():
        return "исчерпан лимит подписки, нужно подождать"
    import claude_quota
    return f"исчерпан лимит до {claude_quota.until_text()}"


def reset() -> None:
    global _pi_until
    _pi_until = 0.0
