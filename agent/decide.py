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
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger("decide")

URL = os.getenv("GLADOS_DECIDER_URL", "http://127.0.0.1:8768")
TIMEOUT = float(os.getenv("GLADOS_DECIDER_TIMEOUT", "2.5"))
EVENT_TIMEOUT_S = 1.0         # ATT-3: событие модуля
INPUT_TIMEOUT_S = 1.5         # ATT-3: обращение пользователя (пользователь ждёт ответа)
MAX_CHARS = 600               # ATT-3: длиннее текст обрезается
MAX_POLICY_OPTIONS = 12      # вариантов в вопросе policy не больше (ТЗ 18)
_DOWN_FOR = 30.0
_down_until = [0.0]
_client: httpx.AsyncClient | None = None


# ── вопросы (ATT-4): метки на английском с описаниями, вход русский ──────────────────────────────

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
    "addressed": {"type": "choice", "options": {
        "assistant": "the text speaks to the AI assistant GLaDOS directly",
        "user": "the text is meant for the human owner",
        "group": "the text is addressed to many people",
        "nobody": "a notification or status, not addressed to anyone"}},
    "needs_reply": {"type": "noul", "instructions": "Does the message expect an answer or an action from its recipient?"},
    # ТЗ 18, первый эшелон фокуса: нужно ли событие вниманию агента прямо сейчас (мимо текущей работы)
    "focus_needed": {"type": "noul", "instructions": "Does this event need the agent's full attention right now, more than its current work?"},
}

NONE_POLICY = "none"          # метка варианта «фокус никому не нужен» в вопросе policy


def event_questions(policy_options: dict | None = None) -> dict:
    """Вопросы о событии. policy_options {"модуль/политика": назначение} - варианты из активных политик модулей (манифесты):
    вопрос «какой модуль и политика подходят событию» (подсказка политики для менеджера фокуса, ТЗ 18). Без вариантов -
    базовые EVENT_QUESTIONS (тот же объект)."""
    if not policy_options:
        return EVENT_QUESTIONS
    options = dict(list(policy_options.items())[:MAX_POLICY_OPTIONS])
    options[NONE_POLICY] = "no module needs the agent's focus for this event"
    return {**EVENT_QUESTIONS, "policy": {"type": "choice", "options": options,
                                          "instructions": "Which module and policy should take the agent's focus for this event?"}}

INPUT_QUESTIONS = {
    "intent": {"type": "choice", "options": {
        "chat": "small talk, opinion, sharing, joking; no task",
        "action": "asks to do something on the computer, in a game, in a messenger, or to find information",
        "about_self": "asks what the assistant can do, how she works, what she remembers",
        "approval": "answers yes or no to a question the assistant asked",
        "attention_rule": "asks to not disturb, to mute or to prioritize something for some time",
        "stop": "asks the assistant to be quiet or stop talking right now"}},
    "needs_memory": {"type": "noul", "instructions": "Does a good reply need facts about people, past events or preferences?"},
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


async def decide(state: str, questions: dict, timeout: float | None = None, down_on_timeout: bool = True) -> dict | None:
    """Ответы на вопросы о тексте: {id: {"answer", "probabilities", "confidence"}} или None, если сервис недоступен.
    down_on_timeout=False: тайм-аут не запоминается как недоступность (сервис жив, но занят очередью)."""
    global _client
    if not available() or not questions:
        return None
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await _client.post(f"{URL}/v1/systemone", json={"state": state, "questions": questions},
                               timeout=timeout or TIMEOUT)
        r.raise_for_status()
        answers = r.json().get("answers")
        return answers if isinstance(answers, dict) else None
    except httpx.TimeoutException as e:
        if down_on_timeout:
            _down_until[0] = time.time() + _DOWN_FOR
        logger.info(f"Классификатор не успел ответить ({type(e).__name__})")
        return None
    except Exception as e:
        _down_until[0] = time.time() + _DOWN_FOR
        logger.info(f"Классификатор недоступен ({e})")
        return None


class Decider:
    """Классификатор для менеджера внимания: текст + вопросы -> ответы или None. Исключений наружу не бросает.

    backend - функция протокола (подменяется заглушкой в тестах): async (state, questions, timeout) -> dict | None."""

    def __init__(self, backend: Callable[..., Awaitable[dict | None]] | None = None):
        self._backend = backend
        self.last_ms: float | None = None          # время последнего вызова, мс

    async def classify(self, text: str, questions: dict, *, timeout: float = EVENT_TIMEOUT_S) -> dict | None:
        text = (text or "").strip()
        if not text or not questions:
            return None
        t0 = time.perf_counter()
        try:
            if self._backend is not None:
                answers = await self._backend(text[:MAX_CHARS], questions, timeout)
            else:
                answers = await decide(text[:MAX_CHARS], questions, timeout=timeout, down_on_timeout=False)
        except Exception as e:
            logger.warning(f"Классификатор: {e}")
            answers = None
        finally:
            self.last_ms = round((time.perf_counter() - t0) * 1000, 1)
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


@dataclass
class EventClass:
    """Что классификатор сказал о событии; поля None, если уверенности не хватило."""

    category: str | None = None
    category_conf: float = 0.0
    addressed: bool | None = None          # к ней ли обращаются (assistant) или нет; None - не уверен
    needs_reply: bool | None = None
    focus_needed: bool | None = None       # нужен ли фокус (ТЗ 18, 1 эшелон); None - не уверен
    focus_conf: float = 0.0
    policy: str | None = None              # подсказка: «модуль/политика» или NONE_POLICY; None - не уверен
    policy_conf: float = 0.0

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
    who, conf = _answer(answers, "addressed")
    if who is not None and conf >= min_conf:
        out.addressed = who == "assistant"
    reply, conf = _answer(answers, "needs_reply")
    if reply in ("yes", "no") and conf >= min_conf:
        out.needs_reply = reply == "yes"
    need, conf = _answer(answers, "focus_needed")
    if need in ("yes", "no") and conf >= min_conf:
        out.focus_needed, out.focus_conf = need == "yes", conf
    pol, conf = _answer(answers, "policy")
    if pol and conf >= min_conf:
        out.policy, out.policy_conf = pol, conf
    return out


@dataclass
class InputClass:
    """Что классификатор сказал об обращении пользователя (ATT-28)."""

    intent: str | None = None              # chat | action | about_self | approval | attention_rule | stop
    intent_conf: float = 0.0
    needs_memory: bool | None = None


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
    return out
