"""Политика фокуса (ТЗ 29, ARC-2, ТЗ 18): правила переключения, старение, дребезг, близость к порогу, конфликт подсказки,
претенденты, эшелон решения. Чистые функции: состояние менеджера фокуса передаётся аргументами, часов и ввода-вывода нет.

Пороги и константы политики живут здесь; `focus.py` импортирует их. Формат `focus.json` и протокол переключения не меняются.
"""
from typing import Any, Iterable

SWITCH_P = 60                  # приоритет, с которого событие переключает фокус (как D3 внимания)
MARGIN = 15                    # запас: событие должно быть выше текущего фокуса на столько (как interrupt_margin)
MIN_FOCUS_S = 20.0             # дребезг: минимум времени в фокусе до переключения (кроме критических)
MAX_SWITCHES_PER_MIN = 4       # дребезг: переключений в минуту
AGING_S = 300.0                # старение: каждые AGING_S ожидания приоритет растёт
AGING_STEP = 10
AGING_CAP = 70                 # старение не поднимает выше этого (критическим оно не нужно)
CONF_LOW = 0.6                 # уверенность классификатора ниже: неоднозначно, спрашиваем Gemma (второй эшелон)
CONF_HIGH = 0.8                # выше: решение по правилам на ответе классификатора, без LLM (первый эшелон)
NONE_HINT = "none"             # подсказка классификатора: фокус никому не нужен
ECHELON_RULES = "rules"        # решили правила: классификатора не было или критическое событие
ECHELON_GLINER = "gliner"      # решили правила на ответе GLiNER (подсказка политики, нужен ли фокус, уверенность)
ECHELON_GEMMA = "gemma"        # решил диспетчер Gemma (принята операция или перепроверка срочности)


def aged_priority(base: float, age_s: float, aging_s: float = AGING_S, step: float = AGING_STEP,
                  cap: float = AGING_CAP) -> float:
    """FOC-3: ожидание поднимает приоритет на `step` за каждые `aging_s` секунд, но не выше `cap`."""
    steps = int(age_s // aging_s)
    return min(cap, base + step * steps) if steps else base


def clears_focus(priority: float, holder_priority: float, switch_p: float = SWITCH_P, margin: float = MARGIN) -> bool:
    """Событие достаточно важно, чтобы переключить фокус, и выше текущего фокуса на запас."""
    return priority >= switch_p and priority >= holder_priority + margin


def dampening(now: float, since: float, switch_times, min_focus_s: float = MIN_FOCUS_S,
              max_per_min: int = MAX_SWITCHES_PER_MIN, window_s: float = 60.0) -> str | None:
    """Дребезг: 'short', если в фокусе мало времени; 'rate', если переключений в окне слишком много; иначе None."""
    if now - since < min_focus_s:
        return "short"
    recent = [t for t in switch_times if now - t < window_s]
    if len(recent) >= max_per_min:
        return "rate"
    return None


def near_threshold(priority: float, holder_priority: float, has_slot: bool, switch_p: float = SWITCH_P,
                   margin: float = MARGIN) -> bool:
    """Близко к порогу переключения или, если фокус занят, к приоритету фокуса."""
    if abs(priority - switch_p) < margin:
        return True
    return has_slot and abs(priority - holder_priority) < margin


def hint_conflicts(hint: str | None, hint_conf: float, priority: float, focus_needed: bool | None, module: str,
                   switch_p: float = SWITCH_P, conf_low: float = CONF_LOW, none_hint: str = NONE_HINT) -> bool:
    """Конфликт подсказки классификатора: другой модуль, либо «фокус не нужен» при высокой важности."""
    if hint is None or hint_conf < conf_low:
        return False
    if hint == none_hint:
        return priority >= switch_p and focus_needed is not False
    return hint.split("/", 1)[0] != module


def has_claimant(priority: float, module: str, event_id: str, others: Iterable[tuple], pending: str,
                 switch_p: float = SWITCH_P, margin: float = MARGIN) -> bool:
    """Два претендента на фокус с близкой важностью от разных модулей. `others`: (id, модуль, статус, критичность,
    приоритет) остальных событий."""
    if priority < switch_p:
        return False
    return any(oid != event_id and omod != module and status == pending and not critical
               and op >= switch_p and abs(op - priority) < margin
               for oid, omod, status, critical, op in others)


def echelon(llm_used: bool, critical: bool, has_classifier_answer: bool) -> str:
    """Какой эшелон решил: Gemma (если ответила операцией или перепроверкой), иначе GLiNER (если давал ответ), иначе правила."""
    if llm_used:
        return ECHELON_GEMMA
    if critical:
        return ECHELON_RULES
    if has_classifier_answer:
        return ECHELON_GLINER
    return ECHELON_RULES


def confirmed(value: Any, policy: str | None) -> bool:
    """Модуль подтвердил переход: ok true и, если назвал политику, то именно запрошенную."""
    if not isinstance(value, dict) or value.get("ok") is not True:
        return False
    return value.get("policy") in (None, policy)


def hint_value(value: Any) -> str | None:
    """Подсказка политики от классификатора: «модуль/политика» или NONE_HINT; мусор отбрасывается."""
    return value if isinstance(value, str) and value and len(value) <= 120 else None
