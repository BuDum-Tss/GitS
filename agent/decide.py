"""Классификатор событий и обращений для менеджера внимания (ТЗ 03, ATT-3, ATT-4).

Модель GLiNER2.5-multi-Decide живёт в отдельном сервисе `services/decider` (порт 8768, свой Python 3.12 с torch CPU):
ядро работает на Python 3.14, где torch может не ставиться, и модель не должна ему мешать. Здесь клиент того же
протокола, что у клиента в `rimworld-module/agent/decider.py` (`POST /v1/systemone`), и класс `Decider` поверх него.

Протокол:
    запрос  {"state": "<текст>", "questions": {"id": {"type": "choice", "options": {"метка": "описание"}}}}
    ответ   {"answers": {"id": {"answer": "<метка>", "confidence": 0.62, "probabilities": {"<метка>": 0.62}}}, "ms": 280}
Типы вопросов: `choice` (options: список или словарь {метка: описание}; модель видит описание) и `noul` (да/нет,
`answer` "yes" или "no"). Проверено на живой модели (CHANGELOG rimworld): категории она различает уверенно, абстрактные
порядковые вопросы («срочность от 0 до 5») почти наугад. Поэтому вопросы только категориальные, а срочность выводит код
по `CATEGORY_URGENCY`.

Нет сервиса или ошибка: `None`, вызывающий код падает на подсказки модуля. Недоступность запоминается на 30 с, чтобы не
стучаться в закрытую дверь на каждом событии. Тайм-аут ответа (сервис занят очередью) недоступностью не считается.
"""
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

import flow_trace
from envcompat import env

logger = logging.getLogger("decide")

URL = env("GITS_DECIDER_URL", "http://127.0.0.1:8768")
TIMEOUT = float(env("GITS_DECIDER_TIMEOUT", "2.5"))
EVENT_TIMEOUT_S = float(env("GITS_DECIDER_EVENT_TIMEOUT", "3.0"))    # ATT-3: событие модуля (замер: ответ 0.6-1 с на процессоре, а вызовы идут по очереди)
INPUT_TIMEOUT_S = float(env("GITS_DECIDER_INPUT_TIMEOUT", "2.5"))   # ATT-3: обращение пользователя (пользователь ждёт ответа)
MAX_CHARS = 600               # ATT-3: длиннее текст обрезается
MAX_POLICY_OPTIONS = 12      # вариантов в вопросе policy не больше (ТЗ 18)
_DOWN_FOR = 30.0
_down_until = [0.0]
_client: httpx.AsyncClient | None = None


# ── вопросы (ATT-4): метки на английском с описаниями, вход русский ──────────────────────────────

# Эмоция реплики или события: стандартный набор (он же набор стикеров питомца, совпадает с fast_brain.EMOTIONS, проверяет тест).
# Выбирает её классификатор вместе с остальными вопросами; уверенность ниже EMOTION_MIN_CONF - эмоции нет (ответ не берётся)
EMOTION_OPTIONS = {
    "idle": "neutral, calm, nothing special",
    "action": "determination, urgency, getting to work",
    "angry": "anger, irritation, annoyance, rudeness",
    "question": "puzzlement, curiosity, being asked something",
    "looking": "attention, watching, noticing something",
    "sitting": "calm waiting, patience, boredom",
    "thinking": "thinking, pondering, weighing something",
    "happy": "joy, delight, amusement, praise, good news",
    "threat": "alarm about danger, attack, threat",
    "resting": "tiredness, relaxation, sleep",
    "sad": "sadness, disappointment, loss, bad news",
    "scared": "fear, fright, panic",
    "proud": "pride, satisfaction, achievement",
    "surprised": "surprise, shock, something unexpected",
}
EMOTION_QUESTION = {"type": "choice", "options": EMOTION_OPTIONS,
                    "instructions": "Which emotion does this text evoke in the assistant?"}
EMOTION_MIN_CONF = 0.3

EVENT_QUESTIONS = {
    "category": {"type": "choice", "options": {
        "danger": "attack, raid, threat to life or property happening now",
        "death_injury": "someone died, was badly hurt or fell ill",
        "disaster": "fire, flood, breakdown, data loss or similar emergency",
        "request_to_assistant": "someone asks the assistant to do something",
        "question": "someone asks a question and waits for an answer",
        "personal_message": "a personal message from a person to the recipient",
        "news": "information, update, announcement",
        "social": "small talk, jokes, greetings, chatter",
        "routine": "status, progress, normal operation, nothing changed",
        "spam": "advertising, scam, automated junk"}},
    # decide это роутер (маршруты `routes.py`): нужна ли реакция вообще и какая, можно несколько сразу
    "react": {"type": "noul", "instructions": "Does the assistant need to react to this event at all?"},
    "speak": {"type": "noul", "instructions": "Should the assistant say something aloud about this?"},
    "act": {"type": "noul", "instructions": "Should the assistant take a quick action in response, not just talk?"},
    "think": {"type": "noul", "instructions": "Does this deserve thinking over later, beyond an instant reaction?"},
    "emotion": EMOTION_QUESTION,
}

NONE_FOCUS = "none"           # метка варианта «оставить фокус как есть» в вопросе active_focus
FOCUS_MIN_CONF = 0.6          # фокус переключается редко: подсказка active_focus принимается только при высокой уверенности


def event_questions(policy_options: dict | None = None) -> dict:
    """Вопросы о событии. policy_options {"модуль/политика": назначение} - варианты из активных политик модулей (манифесты):
    вопрос `active_focus` «чей фокус нужен после события» (rimworld, montage, агент...) с вариантом «оставить как есть»
    (подсказка менеджеру фокуса, ТЗ 18). Без вариантов - базовые EVENT_QUESTIONS (тот же объект)."""
    if not policy_options:
        return EVENT_QUESTIONS
    options = dict(list(policy_options.items())[:MAX_POLICY_OPTIONS])
    options[NONE_FOCUS] = "keep the current focus, nothing needs to switch"
    return {**EVENT_QUESTIONS, "active_focus": {"type": "choice", "options": options,
                                                "instructions": "Which module should hold the agent's focus after this event?"}}

INPUT_QUESTIONS = {
    "intent": {"type": "choice", "options": {
        "chat": "small talk, opinion, sharing, joking; no task",
        "action": "asks to do something on the computer, in a game, in a messenger, or to find information",
        "about_self": "asks what the assistant can do, how she works, what she remembers",
        "approval": "answers yes or no to a question the assistant asked",
        "attention_rule": "asks to not disturb, to mute or to prioritize something for some time",
        "stop": "asks the assistant to be quiet or stop talking right now"}},
    "needs_memory": {"type": "noul", "instructions": "Does a good reply need facts about people, past events or preferences?"},
    "emotion": EMOTION_QUESTION,
}

CATEGORY_URGENCY = {"danger": 5, "death_injury": 4, "disaster": 4, "request_to_assistant": 3, "question": 3,
                    "personal_message": 3, "news": 1, "social": 1, "routine": 0, "spam": 0}


# ── клиент протокола ─────────────────────────────────────────────────────────────────────────────

def choice(options, instructions: str = "") -> dict:
    """Вопрос «выбери один вариант». options: список или словарь {вариант: описание}; описание модель видит вместо имени."""
    return {"type": "choice", "options": dict(options) if isinstance(options, dict) else list(options),
            "instructions": instructions}


def available() -> bool:
    return time.time() >= _down_until[0]


def forget_down() -> None:
    """Сбросить запомненную недоступность (после запуска сервиса и в тестах)."""
    _down_until[0] = 0.0


last_failure: list = [None]       # причина последнего сбоя клиента: "down", "timeout", "error" (ТЗ 17: ошибки GLiNER отдельно); None - успех


async def decide(state: str, questions: dict, timeout: float | None = None, down_on_timeout: bool = True) -> dict | None:
    """Ответы на вопросы о тексте: {id: {"answer", "probabilities", "confidence"}} или None, если сервис недоступен.
    down_on_timeout=False: тайм-аут не запоминается как недоступность (сервис жив, но занят очередью)."""
    global _client
    if not available() or not questions:
        if questions:
            last_failure[0] = "down"
        return None
    last_failure[0] = None
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await _client.post(f"{URL}/v1/systemone", json={"state": state, "questions": questions},
                               timeout=timeout or TIMEOUT)
        r.raise_for_status()
        answers = r.json().get("answers")
        return answers if isinstance(answers, dict) else None
    except httpx.TimeoutException as e:
        last_failure[0] = "timeout"
        if down_on_timeout:
            _down_until[0] = time.time() + _DOWN_FOR
        logger.info(f"Классификатор не успел ответить ({type(e).__name__})")
        return None
    except Exception as e:
        last_failure[0] = "error"
        _down_until[0] = time.time() + _DOWN_FOR
        logger.info(f"Классификатор недоступен ({e})")
        return None


class Decider:
    """Классификатор для менеджера внимания: текст + вопросы -> ответы или None. Исключений наружу не бросает.

    backend - функция протокола (подменяется заглушкой в тестах): async (state, questions, timeout) -> dict | None."""

    def __init__(self, backend: Callable[..., Awaitable[dict | None]] | None = None):
        self._backend = backend
        self.last_ms: float | None = None          # время последнего вызова, мс
        self.last_error: str | None = None         # причина сбоя последнего вызова (down, timeout, error, backend); None - без сбоя

    async def classify(self, text: str, questions: dict, *, timeout: float = EVENT_TIMEOUT_S) -> dict | None:
        text = (text or "").strip()
        if not text or not questions:
            return None
        t0 = time.perf_counter()
        self.last_error = None
        try:
            if self._backend is not None:
                answers = await self._backend(text[:MAX_CHARS], questions, timeout)
            else:
                answers = await decide(text[:MAX_CHARS], questions, timeout=timeout, down_on_timeout=False)
                self.last_error = last_failure[0]
        except Exception as e:
            logger.warning(f"Классификатор: {e}")
            answers = None
            self.last_error = "backend"
        finally:
            self.last_ms = round((time.perf_counter() - t0) * 1000, 1)
            # сбой сервиса пишется отдельным видом `error` (вкладка «Состояние» считает его отдельно от «нет ответа»)
            if self.last_error:
                flow_trace.step(None, "gliner", "bus", "gliner", "error", ms=self.last_ms, size=len(text),
                                label=f"error:{self.last_error}")
            else:
                flow_trace.step(None, "gliner", "bus", "gliner", "classify", ms=self.last_ms, size=len(text),
                                label="ok" if answers else "none")
        return answers if isinstance(answers, dict) and answers else None


# ── разбор ответов ───────────────────────────────────────────────────────────────────────────────

def _answer(answers: dict, qid: str) -> tuple[str | None, float]:
    a = answers.get(qid)
    if not isinstance(a, dict):
        return None, 0.0
    try:
        conf = float(a.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    value = a.get("answer")
    return (str(value) if value is not None else None), conf


def _emotion(answers: dict) -> str | None:
    label, conf = _answer(answers, "emotion")
    return label if label in EMOTION_OPTIONS and label != "idle" and conf >= EMOTION_MIN_CONF else None


@dataclass
class EventClass:
    """Что классификатор сказал о событии; поля None, если уверенности не хватило."""

    category: str | None = None
    category_conf: float = 0.0
    react: bool | None = None              # роутер: реагировать ли вообще; None - не уверен
    speak: bool | None = None              # реплика (маршрут chat)
    act: bool | None = None                # быстрое действие (маршрут reflex)
    think: bool | None = None              # подумать (маршрут think)
    active_focus: str | None = None        # чей фокус нужен: «модуль/политика» или NONE_FOCUS; None - не уверен (порог FOCUS_MIN_CONF)
    focus_conf: float = 0.0
    emotion: str | None = None             # эмоция из EMOTION_OPTIONS, не idle; None - нет или не уверен

    @property
    def urgency(self) -> int | None:
        return CATEGORY_URGENCY.get(self.category) if self.category else None


def parse_event(answers: dict | None, min_conf: float) -> EventClass | None:
    """Ответ на EVENT_QUESTIONS (ATT-8): ответ с уверенностью ниже порога не используется."""
    if not answers:
        return None
    out = EventClass()
    cat, conf = _answer(answers, "category")
    if cat in CATEGORY_URGENCY and conf >= min_conf:
        out.category, out.category_conf = cat, conf
    for name in ("react", "speak", "act", "think"):
        value, conf = _answer(answers, name)
        if value in ("yes", "no") and conf >= min_conf:
            setattr(out, name, value == "yes")
    focus, conf = _answer(answers, "active_focus")
    if focus and conf >= max(min_conf, FOCUS_MIN_CONF):
        out.active_focus, out.focus_conf = focus, conf
    out.emotion = _emotion(answers)
    return out


@dataclass
class InputClass:
    """Что классификатор сказал об обращении пользователя (ATT-28)."""

    intent: str | None = None              # chat | action | about_self | approval | attention_rule | stop
    intent_conf: float = 0.0
    needs_memory: bool | None = None
    emotion: str | None = None             # эмоция обращения из EMOTION_OPTIONS, не idle; None - нет или не уверен


def parse_input(answers: dict | None, min_conf: float) -> InputClass | None:
    """Ответ на INPUT_QUESTIONS: для intent порог уверенности min_conf (0.5), ниже - как будто классификатора нет."""
    if not answers:
        return None
    out = InputClass()
    intent, conf = _answer(answers, "intent")
    if intent in INPUT_QUESTIONS["intent"]["options"] and conf >= min_conf:
        out.intent, out.intent_conf = intent, conf
    mem, conf = _answer(answers, "needs_memory")
    if mem in ("yes", "no") and conf >= min_conf:
        out.needs_memory = mem == "yes"
    out.emotion = _emotion(answers)
    return out
