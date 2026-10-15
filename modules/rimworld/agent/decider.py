"""Клиент быстрой модели решений (Jev-подобной): отвечает на набор вопросов о тексте за один проход, без генерации.

Сервис `decider_server.py` держит GLiNER2.5-Decide (энкодер ~300M, Apache-2.0) на процессоре в своём окружении, чтобы не мешать
Gemma на видеокарте. С ТЗ 11 он живёт в репозитории GitS (`services/decider/`) и запускается супервизором ядра (ТЗ 03), этот
модуль только клиент. Формат запроса и ответа близок к Jev/Kev
(`POST /v1/systemone`), поэтому бэкенд можно заменить, не трогая остальной код.

Запрос:  {"state": "<текст>", "questions": {"id": {"type": "choice", "options": [...], "instructions": "..."}}}
Ответ:   {"answers": {"id": {"answer": "<вариант>", "probabilities": {"вариант": p}, "confidence": p}}, "ms": 123}

Нет сервиса или он ответил с ошибкой: вернётся None, вызывающий код падает на старые правила и Gemma. Временная недоступность
запоминается, чтобы не стучаться в неё на каждом событии."""
import logging
import os
import time

import httpx

logger = logging.getLogger("decider")

URL = os.getenv("GLADOS_DECIDER_URL", "http://127.0.0.1:8768")
TIMEOUT = float(os.getenv("GLADOS_DECIDER_TIMEOUT", "2.5"))
_DOWN_FOR = 30.0
_down_until = [0.0]
_client: httpx.AsyncClient | None = None


def choice(options, instructions: str = "") -> dict:
    """Вопрос «выбери один вариант». options: список или словарь {вариант: описание}; описание модель видит вместо имени."""
    return {"type": "choice", "options": dict(options) if isinstance(options, dict) else list(options), "instructions": instructions}


def available() -> bool:
    return time.time() >= _down_until[0]


async def decide(state: str, questions: dict, timeout: float | None = None) -> dict | None:
    """Ответы на вопросы о тексте: {id: {"answer", "probabilities", "confidence"}} или None, если сервис недоступен."""
    global _client
    if not available() or not questions:
        return None
    if _client is None:
        _client = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await _client.post(f"{URL}/v1/systemone", json={"state": state, "questions": questions}, timeout=timeout or TIMEOUT)
        r.raise_for_status()
        answers = r.json().get("answers")
        if not isinstance(answers, dict):
            return None
        return answers
    except Exception as e:
        _down_until[0] = time.time() + _DOWN_FOR
        logger.info(f"Модель решений недоступна ({e}): использую правила и Gemma")
        return None
