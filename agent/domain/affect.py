"""Эмоция персонажа: чистая логика (ТЗ 26). Без ввода-вывода, настроек и часов: время приходит аргументом.

Эмоция одна и только из стандартного набора (он же набор стикеров питомца). Её выбирает классификатор (GLiNER) по реплике
пользователя или событию. Новая эмоция сразу заменяет прежнюю и держится считанные секунды (настройка `emotion_ttl_s`),
потом остаётся `idle`. Ни валентности, ни возбуждения, ни долгого настроения нет. Эмоция идёт в контекст реплики и
определяет картинку питомца, больше ничего не меняет.
"""
from __future__ import annotations

from dataclasses import dataclass

# Стандартный набор эмоций (стикеры питомца). Совпадает с fast_brain.EMOTIONS и decide.EMOTION_OPTIONS (тест)
LABELS: tuple[str, ...] = ("idle", "action", "angry", "question", "looking", "sitting", "thinking", "happy", "threat",
                           "resting", "sad", "scared", "proud", "surprised")
NEUTRAL = "idle"
DEFAULT_TTL_S = 3.0
TTL_MIN_S, TTL_MAX_S = 0.5, 60.0


def clamp_ttl(value) -> float:
    """Время жизни эмоции из настройки: число от TTL_MIN_S до TTL_MAX_S, иначе значение по умолчанию."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TTL_S
    return max(TTL_MIN_S, min(TTL_MAX_S, float(value)))


@dataclass(frozen=True)
class State:
    emotion: str = NEUTRAL
    ts: float = 0.0                 # когда эмоция возникла
    reason: str = ""                # что её вызвало (имя события или «chat»)


def set_emotion(state: State, label, now: float, reason: str = "") -> State:
    """Новая эмоция перебивает старую. Не из набора и `idle` ничего не меняют (прежняя доживёт свой срок сама)."""
    if label not in LABELS or label == NEUTRAL:
        return state
    return State(emotion=label, ts=now, reason=str(reason or "")[:120])


def current(state: State, ttl: float, now: float) -> str:
    """Эмоция сейчас: пока не вышел срок, иначе idle."""
    if state.emotion != NEUTRAL and 0 <= now - state.ts < ttl:
        return state.emotion
    return NEUTRAL


def emotion_fact(state: State, ttl: float, now: float) -> str:
    """Факт для контекста (AFF-5): какая эмоция заметна сейчас и что её вызвало. Нет свежей эмоции - пусто."""
    label = current(state, ttl, now)
    if label == NEUTRAL:
        return ""
    parts = [f"Сейчас в тебе отзывается эмоция «{label}»"]
    if state.reason:
        parts.append(f"причина: {state.reason}")
    return "; ".join(parts) + ". Это окрашивает тон, но не меняет факты, твои принципы и правила разрешений."
