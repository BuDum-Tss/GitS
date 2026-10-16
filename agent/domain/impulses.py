"""Решения по импульсам и размышлению (ТЗ 30, IMP-3, IMP-7): лимиты, склейка просроченного, допуск действий. Чистые функции."""

MAX_DEPTH = 3            # глубина цепочки импульсов
MAX_PER_HOUR = 6         # размышлений в час
MAX_STEPS = 4            # шагов внутреннего диалога
MAX_TOKENS = 1500        # токенов (оценка) на размышление
LATE_S = 90.0            # опоздание, после которого напоминание считается просроченным
REMINDER_TTL_S = 7 * 24 * 3600.0     # срок жизни напоминания после срока
HOUR_S = 3600.0


def estimate_tokens(text: str) -> int:
    """Грубая оценка токенов по длине текста (контракт модели расхода не отдаёт)."""
    return max(1, len(text) // 3)


def rate_allowed(stamps: list[float], now: float, per_hour: int) -> bool:
    """Можно ли начать ещё одно размышление: за последний час их было меньше лимита."""
    return sum(1 for t in stamps if now - t < HOUR_S) < per_hour


def depth_allowed(depth: int, max_depth: int = MAX_DEPTH) -> bool:
    """Можно ли породить напоминание из события глубины `depth`."""
    return depth + 1 <= max_depth


def split_due(due: list[tuple[str, float]], now: float, late_s: float = LATE_S) -> tuple[list[str], list[str]]:
    """(вовремя, просрочено) по парам (id, срок). Просроченные выдаются одним событием (склейка)."""
    on_time = [i for i, d in due if now - d <= late_s]
    late = [i for i, d in due if now - d > late_s]
    return on_time, late


def expired(due: float, now: float, ttl_s: float = REMINDER_TTL_S) -> bool:
    return now - due > ttl_s


def allow_do(trusted: bool) -> bool:
    """Действие по размышлению исполняется только по доверенному стимулу; чужие тексты остаются данными (IMP-7)."""
    return trusted
