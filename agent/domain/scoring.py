"""Оценка события для внимания (ТЗ 29, ARC-2): приоритет 0..100, свежесть, решение D0-D4, период полураспада.

Чистые функции: веса профиля, отправителя, правил и состояния пользователя считает класс внимания и передаёт готовыми.
"""
import fnmatch

DECISION_NAMES = ("D0", "D1", "D2", "D3", "D4")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def longest_match(patterns: dict, kind: str, default=None):
    """Значение для самого длинного шаблона (fnmatch), совпавшего с видом события."""
    best, best_len = default, -1
    for pat, value in patterns.items():
        if len(pat) > best_len and fnmatch.fnmatchcase(kind, pat):
            best, best_len = value, len(pat)
    return best


def priority(urgency: float, w_profile: float, w_sender: float, w_rule: float, w_user: float,
             freshness: float) -> float:
    """Приоритет: срочность x 20, умноженная на веса и свежесть, ограниченная 0..100."""
    value = urgency * 20.0
    for w in (w_profile, w_sender, w_rule, w_user, freshness):
        value *= w
    return _clamp(value, 0.0, 100.0)


def freshness(age_s: float, half_life_s: float) -> float:
    """Свежесть: 1.0 сразу, половина через каждый период полураспада. Отрицательный возраст считается нулевым."""
    return 0.5 ** (max(0.0, age_s) / half_life_s)


def decide(priority: float, thresholds, away: bool, top_focus_kind: str, top_focus_priority: float,
           interrupt_margin: float) -> str:
    """Уровень D0-D4 по порогам. Высокая важность поверх чужого фокуса поднимается до D4; отошедшему пользователю
    всё важное, кроме D0-D1, отдаётся как D1 (в сводку)."""
    t = thresholds
    level = sum(1 for th in t if priority >= th)             # 0..4
    if level >= 2 and priority >= t[2]:
        if priority >= t[3] or (top_focus_kind != "idle" and priority > top_focus_priority + interrupt_margin):
            level = 4
    if away and level >= 2:
        level = 1                                             # сказать некому: в сводку
    return DECISION_NAMES[level]


def half_life(kind: str, table: dict, default: float) -> float:
    """Период полураспада по виду события: самый длинный подходящий шаблон таблицы; нечисловые и неположительные значения пропускаются."""
    value = longest_match({k: v for k, v in table.items() if isinstance(v, (int, float)) and v > 0}, kind, default)
    return float(value)
