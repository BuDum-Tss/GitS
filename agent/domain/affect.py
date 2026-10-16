"""Эмоции персонажа: чистая логика состояния (ТЗ 26). Без ввода-вывода, настроек и часов: время приходит аргументом.

Состояние: валентность (-1..1), возбуждение (0..1), быстрые эмоции (подпись -> сила 0..1, затухают за минуты) и медленное
настроение (-1..1, затухает за часы). Параметры персонажа читаются из поля `emotions` его карточки; пустое поле даёт
нейтральные значения по умолчанию. Значений конкретного персонажа здесь нет.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

HOUR = 3600.0
# подпись из набора питомца -> (знак валентности -1..1, возбуждение 0..1). Набор совпадает с fast_brain.EMOTIONS (тест)
VECTORS: dict[str, tuple[float, float]] = {
    "idle": (0.0, 0.2), "action": (0.2, 0.8), "angry": (-0.8, 0.85), "question": (0.0, 0.5), "looking": (0.0, 0.45),
    "sitting": (0.1, 0.1), "thinking": (0.0, 0.4), "happy": (0.8, 0.6), "threat": (-0.7, 0.9), "resting": (0.2, 0.05),
    "sad": (-0.8, 0.2), "scared": (-0.7, 0.9), "proud": (0.7, 0.55), "surprised": (0.1, 0.8),
}
NEUTRAL_LABELS = ("idle", "sitting", "resting", "question", "looking", "thinking")   # фоновые: состояние не двигают
INTENSITY_MAX = 3
MOOD_GAIN = 0.3                  # какая доля сдвига валентности попадает в настроение
MOOD_LABEL_AT = 0.2              # |настроение| выше этого даёт подпись настроения


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _num(value, default: float, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return _clamp(float(value), lo, hi)


@dataclass(frozen=True)
class Params:
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.2
    emotion_decay_min: float = 10.0        # постоянная времени быстрых эмоций, минуты
    mood_decay_h: float = 6.0              # постоянная времени настроения, часы
    inertia: float = 0.3                   # 0..0.9: доля сдвига, которую состояние гасит
    max_shift_per_hour: float = 0.6        # максимум суммарного |сдвига валентности| за час
    sensitivity: dict = field(default_factory=dict)   # категория события -> множитель
    voice_rate_limit: float = 8.0          # предел сдвига скорости голоса, %
    voice_pitch_limit: float = 10.0        # предел сдвига тона голоса, Гц
    proactivity_limit: float = 8.0         # предел сдвига порогов внимания, пункты

    def sens(self, category: str) -> float:
        return float(self.sensitivity.get(category, self.sensitivity.get("default", 1.0)))

    @classmethod
    def from_card(cls, raw: dict | None) -> Params:
        raw = raw if isinstance(raw, dict) else {}
        sens = raw.get("sensitivity") if isinstance(raw.get("sensitivity"), dict) else {}
        d = cls()
        return cls(
            baseline_valence=_num(raw.get("baseline_valence"), d.baseline_valence, -1, 1),
            baseline_arousal=_num(raw.get("baseline_arousal"), d.baseline_arousal, 0, 1),
            emotion_decay_min=_num(raw.get("emotion_decay_min"), d.emotion_decay_min, 0.1, 24 * 60),
            mood_decay_h=_num(raw.get("mood_decay_h"), d.mood_decay_h, 0.05, 24 * 14),
            inertia=_num(raw.get("inertia"), d.inertia, 0, 0.9),
            max_shift_per_hour=_num(raw.get("max_shift_per_hour"), d.max_shift_per_hour, 0.01, 2),
            sensitivity={str(k): _num(v, 1.0, 0, 5) for k, v in sens.items()},
            voice_rate_limit=_num(raw.get("voice_rate_limit"), d.voice_rate_limit, 0, 25),
            voice_pitch_limit=_num(raw.get("voice_pitch_limit"), d.voice_pitch_limit, 0, 40),
            proactivity_limit=_num(raw.get("proactivity_limit"), d.proactivity_limit, 0, 20))


@dataclass
class State:
    valence: float = 0.0
    arousal: float = 0.2
    mood: float = 0.0
    emotions: dict = field(default_factory=dict)      # подпись -> сила 0..1
    ts: float = 0.0                                   # на какой момент посчитано
    reason: str = ""                                  # причина последнего заметного сдвига
    ledger: list = field(default_factory=list)        # [[ts, |сдвиг валентности|]] за последний час

    def to_dict(self) -> dict:
        return {"valence": self.valence, "arousal": self.arousal, "mood": self.mood, "emotions": dict(self.emotions),
                "ts": self.ts, "reason": self.reason, "ledger": [list(x) for x in self.ledger]}

    @classmethod
    def from_dict(cls, raw: dict | None, params: Params, now: float) -> State:
        """Битые или чужие данные дают состояние базового уровня."""
        if not isinstance(raw, dict):
            return initial(params, now)
        emotions = raw.get("emotions") if isinstance(raw.get("emotions"), dict) else {}
        ledger = []
        for e in raw.get("ledger") or []:
            if isinstance(e, (list, tuple)) and len(e) == 2 and all(isinstance(x, (int, float)) for x in e):
                ledger.append([float(e[0]), float(e[1])])
        return cls(valence=_num(raw.get("valence"), params.baseline_valence, -1, 1),
                   arousal=_num(raw.get("arousal"), params.baseline_arousal, 0, 1),
                   mood=_num(raw.get("mood"), params.baseline_valence, -1, 1),
                   emotions={str(k): _num(v, 0, 0, 1) for k, v in emotions.items() if k in VECTORS},
                   ts=_num(raw.get("ts"), now, 0, 1e12), reason=str(raw.get("reason") or "")[:120], ledger=ledger)


def initial(params: Params, now: float) -> State:
    return State(valence=params.baseline_valence, arousal=params.baseline_arousal, mood=params.baseline_valence, ts=now)


def decay(state: State, params: Params, now: float) -> State:
    """Затухание к базовому уровню за время с `state.ts` (AFF-4): точная экспонента, поэтому результат не зависит от того,
    считали ли по шагам или одним большим шагом (пересчёт при пробуждении). Время назад ничего не меняет."""
    dt = now - state.ts
    if dt <= 0:
        return state
    fast = math.exp(-dt / (params.emotion_decay_min * 60.0))
    slow = math.exp(-dt / (params.mood_decay_h * HOUR))
    b_v, b_a = params.baseline_valence, params.baseline_arousal
    emotions = {k: v * fast for k, v in state.emotions.items() if v * fast >= 0.01}
    ledger = [e for e in state.ledger if now - e[0] < HOUR]
    return State(valence=b_v + (state.valence - b_v) * fast, arousal=b_a + (state.arousal - b_a) * fast,
                 mood=b_v + (state.mood - b_v) * slow, emotions=emotions, ts=now, reason=state.reason, ledger=ledger)


def budget_left(state: State, params: Params, now: float) -> float:
    used = sum(a for t, a in state.ledger if now - t < HOUR)
    return max(0.0, params.max_shift_per_hour - used)


def appraise(state: State, params: Params, now: float, *, label: str, intensity, category: str = "default",
             valence_push: float = 0.0, reason: str = "") -> tuple[State, list[str]]:
    """Сдвиг состояния событием (AFF-3). Сдвиг = интенсивность(0..3)/3 x знак x чувствительность, с инерцией и насыщением.
    label: подпись события (из набора питомца); valence_push: добавочный знак (например, изменение отношений, -1..1).
    Возвращает (новое состояние, предупреждения). Значения обрезаются по границам (AFF-6): за час не больше
    max_shift_per_hour суммарного сдвига валентности."""
    warnings: list[str] = []
    state = decay(state, params, now)
    if isinstance(intensity, bool) or not isinstance(intensity, (int, float)):
        intensity = 1
    strength = _clamp(float(intensity), 0, INTENSITY_MAX) / INTENSITY_MAX
    sign, arousal_target = VECTORS.get(label, (0.0, params.baseline_arousal))
    if label in NEUTRAL_LABELS and not valence_push:
        return state, warnings
    sens = params.sens(category)
    keep = 1.0 - params.inertia
    raw = (strength * sign + _clamp(valence_push, -1, 1)) * sens * keep * 0.5
    headroom = (1.0 - state.valence) if raw > 0 else (1.0 + state.valence)    # насыщение: к границе сдвиг меньше
    shift = raw * _clamp(headroom, 0.0, 2.0)
    left = budget_left(state, params, now)
    if abs(shift) > left:
        warnings.append(f"сдвиг {shift:+.2f} обрезан до {math.copysign(left, shift):+.2f}: лимит {params.max_shift_per_hour} в час")
        shift = math.copysign(left, shift)
    new_v = _clamp(state.valence + shift, -1, 1)
    applied = new_v - state.valence
    arousal = _clamp(state.arousal + (arousal_target - state.arousal) * strength * sens * keep, 0, 1)
    emotions = dict(state.emotions)
    if label in VECTORS and label not in NEUTRAL_LABELS:
        emotions[label] = _clamp(max(emotions.get(label, 0.0), strength) + 0.1 * strength, 0, 1)
    ledger = state.ledger + ([[now, abs(applied)]] if applied else [])
    mood = _clamp(state.mood + applied * MOOD_GAIN, -1, 1)
    return State(valence=new_v, arousal=arousal, mood=mood, emotions=emotions, ts=now,
                 reason=(reason or label) if abs(applied) >= 0.01 else state.reason, ledger=ledger), warnings


def dominant(state: State) -> str:
    """Подпись для питомца из набора: сильнейшая быстрая эмоция; иначе по настроению; иначе idle."""
    if state.emotions:
        label, power = max(state.emotions.items(), key=lambda kv: kv[1])
        if power >= 0.15:
            return label
    if state.mood <= -MOOD_LABEL_AT:
        return "sad"
    if state.mood >= MOOD_LABEL_AT:
        return "happy"
    return "idle"


def mood_word(state: State) -> str:
    m = state.mood
    if m <= -0.6:
        return "очень плохое"
    if m <= -MOOD_LABEL_AT:
        return "плохое"
    if m < MOOD_LABEL_AT:
        return "ровное"
    if m < 0.6:
        return "хорошее"
    return "очень хорошее"


def mood_fact(state: State, params: Params, now: float) -> str:
    """Факт для контекста (AFF-5): состояние и причина, без готовых реплик."""
    state = decay(state, params, now)
    level = "высокое" if state.arousal > 0.65 else "среднее" if state.arousal > 0.3 else "низкое"
    parts = [f"Твоё настроение сейчас {mood_word(state)}, возбуждение {level}"]
    if state.emotions:
        label, power = max(state.emotions.items(), key=lambda kv: kv[1])
        if power >= 0.15:
            parts.append(f"заметна эмоция «{label}»")
    if state.reason and (abs(state.mood - params.baseline_valence) >= 0.05 or state.emotions):
        parts.append(f"причина: {state.reason}")
    return "; ".join(parts) + ". Это окрашивает тон, но не меняет факты, твои принципы и правила разрешений."


def voice_tone(state: State, params: Params) -> dict:
    """Ограниченный сдвиг голоса: скорость в % и тон в Гц. Хорошее настроение и возбуждение быстрее и выше."""
    k = _clamp(state.valence - params.baseline_valence, -1, 1)
    a = _clamp(state.arousal - params.baseline_arousal, -1, 1)
    rate = _clamp((0.6 * a + 0.4 * k) * params.voice_rate_limit, -params.voice_rate_limit, params.voice_rate_limit)
    pitch = _clamp((0.5 * a + 0.5 * k) * params.voice_pitch_limit, -params.voice_pitch_limit, params.voice_pitch_limit)
    return {"rate_pct": round(rate, 1), "pitch_hz": round(pitch, 1)}


def proactivity_offset(state: State, params: Params) -> float:
    """Сдвиг порогов внимания в пунктах: хорошее настроение снижает пороги (охотнее говорит), плохое повышает."""
    k = _clamp(state.mood - params.baseline_valence, -1, 1)
    return round(-k * params.proactivity_limit, 2)
