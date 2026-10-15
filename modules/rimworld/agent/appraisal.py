"""Динамическая оценка событий: быстрая модель решений (decider.py) отвечает на набор вопросов о событии, а общее
«настроение» (Affect) копит оценки с затуханием и подсказывает и модели решений, и Gemma.

Зачем. Раньше эмоцию и рефлекс выбирала сама Gemma по одному событию: медленно и без памяти о том, что только что было.
Теперь на каждое событие за ~0.2-0.5 с приходит оценка: насколько важно, какая эмоция, нужен ли рефлекс (draft/undraft),
стоит ли комментировать и как развёрнуто, новизна (повтор ли), нарастает ли напряжение, нужен ли Claude. Оценка видит
контекст (последние события, серия однотипных, напряжение), а Gemma получает её как подсказку тона: она реагирует по
ситуации, а не шаблонно, молчит на рутине и меняет интонацию при нарастании.

Модель не рассуждает и не пишет текст: она только выбирает варианты. Всё исполняемое (рефлексы) по-прежнему проходит
проверку по реальному состоянию игры (reflex_actions.check), так что ошибка оценки не опасна."""
import time
from collections import deque
from dataclasses import dataclass, field

import decider

IMPORTANCE = ["trivial", "minor", "notable", "serious", "critical"]
VALENCE = ["negative", "neutral", "positive"]
EMOTIONS = ["idle", "action", "angry", "question", "looking", "sitting", "thinking", "happy", "threat", "resting",
            "sad", "scared", "proud", "surprised"]
ACTIONS = ["none", "draft_colonists", "undraft"]
COMMENT = ["silent", "brief", "full"]
NOVELTY = ["new", "related_to_recent", "repeated"]
TREND = ["calming", "steady", "escalating"]
CLAUDE = ["no", "next_turn", "urgent"]

HALF_LIFE = 150.0        # секунд: за столько напряжение уменьшается вдвое
STREAK_WINDOW = 600.0    # серия однотипных событий считается в этом окне
_FORCE_MIN = {"colony_died": 4, "pawn_died": 3}     # такие события не бывают рутиной, что бы ни ответила модель


# Проверено на живой модели: она уверенно различает КОНКРЕТНЫЕ категории («бой», «смерть», «пожар», «торговля»: 10 из 10 верно,
# уверенность 0.5-1.0) и да/нет про опасность, но почти случайно отвечает на абстрактные порядковые вопросы («насколько важно»,
# «повтор ли», «нарастает ли»). Поэтому модель только классифицирует, а важность, повтор, динамику, рефлекс и длину реплики
# выводит код (derive) из категории, опасности, срочности, тона, серии однотипных и общего напряжения.
KINDS = {                      # ключ для кода: фраза для модели
    "combat": "combat threat, raid or attack",
    "death": "death of a colonist",
    "injury": "injury or illness",
    "fire": "fire or disaster",
    "trade": "trade, visitors or caravan",
    "social": "social event, friendship, romance or conflict",
    "work": "construction or work finished",
    "resources": "resources, food or storage",
    "other": "something else",
}
URGENCY = {"routine": "routine", "important": "important", "emergency": "emergency"}
TONE = {"bad": "bad news", "neutral": "neutral information", "good": "good news"}
_EMOTION_OF = {"combat": "threat", "death": "sad", "injury": "scared", "fire": "scared", "trade": "question",
               "social": "happy", "work": "proud", "resources": "looking", "other": "looking"}
_BASE_IMPORTANCE = {"combat": 3, "death": 3, "injury": 2, "fire": 3, "resources": 1, "trade": 0, "social": 0, "work": 0, "other": 0}
MIN_KIND_CONFIDENCE = 0.35      # ниже этой уверенности категорию не берём (калибровка: до 0.4 точность ~56%, от 0.6 ~92-100%)


def questions() -> dict:
    """Конкретные вопросы к каждому событию (по-английски: с английскими метками модель работает лучше всего)."""
    d = decider.choice
    return {
        "kind": d(KINDS, "What kind of event is this?"),
        "danger": {"type": "noul", "instructions": "Is there an attack, fire or other immediate danger to the colonists?"},
        "tone": d(TONE, "Is this good or bad news for the colony?"),
    }


@dataclass
class Appraisal:
    importance: int = 1
    valence: str = "neutral"
    emotion: str = "looking"
    action: str = "none"
    comment: str = "brief"
    novelty: str = "new"
    trend: str = "steady"
    claude: str = "no"
    confidence: float = 0.0
    source: str = "decider"
    ms: int = 0
    kind: str = ""
    ts: float = field(default_factory=time.time)
    log_id: str = ""

    def tone(self) -> str:
        """Подсказка Gemma, как отреагировать (коротко, по-русски, без готовых реплик)."""
        length = {"silent": "можно промолчать", "brief": "одна короткая реплика", "full": "развёрнутая реакция в 2-3 предложения"}[self.comment]
        parts = [f"важность {self.importance}/4", f"эмоция {self.emotion}", length]
        if self.novelty == "repeated":
            parts.append("это повтор: не повторяйся, добавь новое или промолчи")
        elif self.novelty == "related_to_recent":
            parts.append("продолжение недавнего: опирайся на него")
        if self.trend == "escalating":
            parts.append("напряжение нарастает")
        elif self.trend == "calming":
            parts.append("напряжение спадает")
        return ", ".join(parts)


class Affect:
    """Общее настроение: напряжение и знак с затуханием, лента последних событий, серии однотипных."""

    def __init__(self):
        self.arousal = 0.0
        self.valence = 0.0
        self._ts = time.time()
        self.events: deque = deque(maxlen=14)     # (ts, kind, text, importance)
        self.last: Appraisal | None = None

    def _decay(self, now: float):
        k = 0.5 ** (max(0.0, now - self._ts) / HALF_LIFE)
        self.arousal *= k
        self.valence *= k
        self._ts = now

    def streak(self, kind: str, now: float | None = None) -> int:
        now = now or time.time()
        return sum(1 for ts, k, _t, _i in self.events if k == kind and now - ts < STREAK_WINDOW)

    def update(self, ap: Appraisal, text: str, now: float | None = None):
        now = now or time.time()
        self._decay(now)
        self.arousal = min(1.0, self.arousal + 0.18 * ap.importance)
        self.valence = max(-1.0, min(1.0, self.valence + {"negative": -0.3, "neutral": 0.0, "positive": 0.3}[ap.valence] * (ap.importance + 1) / 3))
        self.events.append((now, ap.kind, text[:120], ap.importance))
        self.last = ap

    def context(self, now: float | None = None) -> str:
        """Текст для модели решений: напряжение и недавние события (по-английски метки, русские описания как есть)."""
        now = now or time.time()
        self._decay(now)
        lines = [f"tension {self.arousal:.2f}, mood {self.valence:+.2f}"]
        for ts, kind, text, imp in list(self.events)[-6:]:
            lines.append(f"{int(now - ts)}s ago [{kind}, importance {imp}]: {text}")
        return "\n".join(lines)

    def mood_hint(self, now: float | None = None) -> str:
        """Подсказка Gemma об общем настроении (для реплик и разговора)."""
        now = now or time.time()
        self._decay(now)
        if self.arousal < 0.15 and abs(self.valence) < 0.15:
            return ""
        level = "очень высокое" if self.arousal > 0.7 else "высокое" if self.arousal > 0.4 else "лёгкое"
        sign = "тревожное" if self.valence < -0.2 else "приподнятое" if self.valence > 0.2 else "ровное"
        return f"Общее настроение: напряжение {level}, фон {sign}."


AFFECT = Affect()


def _pick(answer: dict | None, options, default: str) -> tuple[str, float]:
    if not isinstance(answer, dict):
        return default, 0.0
    label = answer.get("answer") or answer.get("choice")
    return (label if label in options else default), float(answer.get("confidence") or 0.0)


def _yes(answer: dict | None) -> float:
    """Вероятность «да» в ответе на noul-вопрос (или 0)."""
    if not isinstance(answer, dict):
        return 0.0
    if "noul" in answer:
        return float(answer["noul"])
    return float(answer.get("confidence") or 0.0) if answer.get("answer") == "yes" else 0.0


def derive(answers: dict, kind: str, text: str, aff: "Affect", now: float | None = None) -> Appraisal:
    """Ответы модели + знания кода -> Appraisal. Модель сказала, ЧТО это за событие и опасно ли; всё остальное считает код."""
    now = now or time.time()
    ek, kind_conf = _pick(answers.get("kind"), KINDS, "other")
    if kind_conf < MIN_KIND_CONFIDENCE:
        ek = "other"                               # не уверена: не выдумываем категорию
    danger = _yes(answers.get("danger"))
    tone, conf = _pick(answers.get("tone"), TONE, "neutral")
    streak = aff.streak(kind, now)
    arousal = aff.arousal

    imp = _BASE_IMPORTANCE.get(ek, 0)
    if danger >= 0.6 and ek in ("combat", "fire", "injury"):
        imp += 1
    if streak >= 3:
        imp += 1                                   # серия однотипных: уже не случайность
    imp = max(_FORCE_MIN.get(kind, 0), min(4, imp))

    ap = Appraisal(importance=imp, kind=kind, confidence=conf)
    # знак по категории (модель плохо различает «хорошо/плохо»: отвечала нейтрально на всё); тон модели только для «другого»
    ap.valence = {"combat": "negative", "death": "negative", "injury": "negative", "fire": "negative",
                  "social": "positive", "work": "positive"}.get(ek) or {"bad": "negative", "good": "positive"}.get(tone, "neutral")
    ap.emotion = _EMOTION_OF.get(ek, "looking")
    if ek == "combat" and arousal < 0.25 and imp < 4:
        ap.emotion = "threat"
    elif ek in ("combat", "fire") and (arousal > 0.6 or streak >= 2):
        ap.emotion = "scared" if ap.valence != "positive" else "angry"
    if ek == "work" and ap.valence == "negative":
        ap.emotion = "idle"
    ap.action = "draft_colonists" if ek == "combat" and danger >= 0.5 else "none"      # исполнится только при реальных врагах (reflex_actions.check)
    # новизна: похожее событие в ленте или серия
    from memory_store import stems
    new = stems(text)
    similar = any(len(new & old) / max(1, min(len(new), len(old))) >= 0.6
                  for old in (stems(t) for _ts, k, t, _i in aff.events if k == kind) if old and new)
    ap.novelty = "repeated" if (streak >= 3 or (similar and streak >= 1)) else "related_to_recent" if streak >= 1 else "new"
    ap.trend = "escalating" if (streak >= 2 and imp >= 3) or (arousal > 0.65 and imp >= 2) else "calming" if arousal < 0.12 and streak == 0 else "steady"
    ap.comment = "silent" if imp <= 1 and ap.novelty != "new" else "full" if imp >= 3 and ap.novelty != "repeated" else "brief"
    if imp <= 1 and ap.novelty == "repeated":
        ap.comment = "silent"
    ap.claude = "urgent" if imp >= 4 or (imp >= 3 and ek in ("combat", "fire") and ap.trend == "escalating") else "next_turn" if imp >= 3 else "no"
    return ap


async def appraise(kind: str, text: str, now: float | None = None) -> Appraisal | None:
    """Оценить событие (kind - тип, text - описание). None, если модель решений недоступна."""
    now = now or time.time()
    state = text          # модели только само событие: история и напряжение в тексте сбивают классификацию (проверено), их учитывает код
    t0 = time.perf_counter()
    answers = await decider.decide(state, questions())
    if answers is None:
        return None
    ap = derive(answers, kind, text, AFFECT, now)
    ap.ms = round((time.perf_counter() - t0) * 1000)
    import appraisal_log
    ap.log_id = appraisal_log.append(kind, text, answers, {
        "importance": ap.importance, "emotion": ap.emotion, "action": ap.action, "comment": ap.comment, "novelty": ap.novelty,
        "trend": ap.trend, "claude": ap.claude, "valence": ap.valence, "ms": ap.ms})
    AFFECT.update(ap, text, now)
    return ap


def recent(max_age: float = 90.0) -> Appraisal | None:
    """Последняя свежая оценка (для комментатора)."""
    ap = AFFECT.last
    return ap if ap and time.time() - ap.ts < max_age else None
