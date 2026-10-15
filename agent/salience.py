"""Яркость события (ТЗ 24, TRC-4): насколько событие стоит запомнить подробно.

Событие получает `data.salience` (0..1) и `data.reasons` (какие факторы сработали). Веса - таблица WEIGHTS: меняются здесь,
без правки логики. Факторы считает вызывающий код из того, что у него есть:
  importance       - срочность внимания (0..5, делится на 5)
  appraisal        - оценка события модулем игры (0..4, делится на 4); поле data.appraisal_importance, если модуль его прислал
  relation         - изменение отношений (|delta| 0..10, делится на 10)
  explicit_memory  - явная просьба «запомни» (или событие с отметкой memorable)
  outcome          - итог: набег, гибель, конец забега, задача выполнена или провалена (см. is_outcome)
  novelty          - новый человек (первый контакт)
"""

WEIGHTS = {
    "importance": 0.35,
    "appraisal": 0.25,
    "relation": 0.15,
    "explicit_memory": 0.5,
    "outcome": 0.5,
    "novelty": 0.2,
}

# виды событий, которые являются итогом (суффиксы вида модуля; например rimworld.run_ended)
OUTCOME_SUFFIXES = (".run_ended", ".colony_died", ".pawn_died", ".death", ".raid_ended", ".raid_over", ".task_done",
                    ".task_failed")


def is_outcome(kind: str) -> bool:
    return str(kind or "").endswith(OUTCOME_SUFFIXES)


def _clamp(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, x))


def score(factors: dict) -> tuple[float, list[str]]:
    """(яркость 0..1, причины): причины - имена факторов с ненулевым вкладом, в порядке таблицы."""
    total, reasons = 0.0, []
    for name, weight in WEIGHTS.items():
        v = _clamp(factors.get(name))
        if v > 0:
            total += weight * v
            reasons.append(name)
    return round(min(1.0, total), 3), reasons


def factors_of(*, urgency=None, appraisal=None, delta=None, memorable=False, kind: str = "", novelty=False) -> dict:
    """Факторы из обычных полей события. None - фактора нет."""
    f = {"explicit_memory": 1.0 if memorable else 0.0, "outcome": 1.0 if is_outcome(kind) else 0.0,
         "novelty": 1.0 if novelty else 0.0}
    if urgency is not None:
        f["importance"] = _clamp(float(urgency) / 5.0)
    if appraisal is not None:
        f["appraisal"] = _clamp(float(appraisal) / 4.0)
    if delta is not None:
        f["relation"] = _clamp(abs(float(delta)) / 10.0)
    return f
