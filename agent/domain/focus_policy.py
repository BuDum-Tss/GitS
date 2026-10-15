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
CRITICAL_URGENCY = 5           # срочность 5 - критическое: без дребезга, без LLM, без ожидания
USER_PRIORITY = 95             # просьба пользователя (закрепить, «возьми фокус»)
HOLDER_PRIORITY_EVENT = 60     # фокус, взятый по событию, держит приоритет 60: обычные события его не сдвигают
RECHECK_CONF = 0.5             # опасное событие с уверенностью классификатора ниже этого перепроверяется
DANGER_CATEGORIES = ("danger", "death_injury", "disaster")


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


def holder_priority(origin: str | None) -> float:
    """Приоритет, который держит фокус: просьба пользователя сильнее фокуса, взятого по событию; без фокуса 0."""
    if origin is None:
        return 0.0
    return float(USER_PRIORITY if origin == "user" else HOLDER_PRIORITY_EVENT)


def needs_recheck(category: str | None, confidence: float | None, rechecked: bool) -> bool:
    """FOC-3: опасное событие с низкой уверенностью классификатора перепроверяется один раз."""
    return (category in DANGER_CATEGORIES and confidence is not None and confidence < RECHECK_CONF
            and not rechecked)


def ambiguity(*, critical: bool, has_llm: bool, llm_used: bool, confidence: float | None, near: bool, conflict: bool,
              claimant: bool, hint: str | None, conf_low: float = CONF_LOW, conf_high: float = CONF_HIGH) -> str | None:
    """Почему событие неоднозначно (нужен второй эшелон) или None, если правила решают сами (ТЗ 18, FOC-7): низкая
    уверенность классификатора, близость к порогу без уверенного ответа, конфликт подсказки, два претендента.
    Критические события, отсутствие LLM и уже отвеченные диспетчером не спрашиваем."""
    if critical or not has_llm or llm_used:
        return None
    if confidence is not None and confidence < conf_low:
        return f"низкая уверенность классификатора ({confidence:.2f})"
    if near and (confidence is None or confidence < conf_high):
        return "у порога переключения или у фокуса, без уверенного ответа классификатора"
    if conflict:
        return f"конфликт подсказки: классификатор указывает «{hint}»"
    if claimant:
        return "два претендента на фокус с близкой важностью"
    return None


def gate(*, critical: bool, once: bool, quiet: bool, profile_quiet: bool, quiet_policy: str | None,
         pinned: str | None, pinned_title: str, module: str, busy: bool, focus_needed: bool | None, focus_conf: float,
         priority: float, has_slot: bool, slot_since: float, switch_times, now: float, holder: float,
         interruptible: bool) -> tuple[str, str, dict]:
    """Детерминированные правила (FOC-4, FOC-6): что делать с событием. Возвращает (действие, причина, условие
    возобновления). `quiet`: включено «не беспокоить» для всего агента или для модуля; `quiet_policy`: id текущей
    политики модуля, которая не переключает фокус (или None)."""
    ctx = {"type": "context_changed"}
    if critical:
        return "switch", "критическое событие: переключение без ожидания", ctx
    if not once:
        if quiet:
            return "defer", "не беспокоить: режим пользователя", {"type": "context_changed"}
        if profile_quiet:
            return "defer", "профиль не даёт переключать фокус этому модулю", ctx
        if quiet_policy is not None:
            return "defer", f"политика «{quiet_policy}» не переключает фокус: сообщение сохранено", ctx
    if pinned is not None and module != pinned:
        return "defer", f"фокус закреплён за «{pinned_title}»", {"type": "unpin"}
    if busy:
        return "defer", "пользователь в разговоре", ctx
    if focus_needed is False and focus_conf >= CONF_HIGH:
        return "defer", "классификатор: событие не требует фокуса", ctx
    if not has_slot:
        if priority >= SWITCH_P:
            return "switch", f"приоритет {round(priority)} не ниже порога {SWITCH_P}", ctx
        return "defer", f"приоритет {round(priority)} ниже порога переключения {SWITCH_P}", ctx
    damp = dampening(now, slot_since, switch_times)
    if damp == "short":
        return "defer", "дребезг: мало времени в текущем фокусе", ctx
    if damp == "rate":
        return "defer", "дребезг: лимит переключений в минуту", ctx
    if not clears_focus(priority, holder):
        return "defer", f"не выше текущего фокуса на запас {MARGIN}", ctx
    if not interruptible:
        return "defer", "модуль в фокусе сообщил небезопасную точку", ctx
    return "switch", f"приоритет {round(priority)} выше фокуса {round(holder)} на запас", ctx
