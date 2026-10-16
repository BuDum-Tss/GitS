"""Planner (ТЗ 32, WS-8): разбиение цели на шаги через локальную модель (контракт `LanguageModel`). Одинаков для всех сред.

Среда лишь подсказывает контекст (название, назначение, инструменты); сама не планирует. Цель могла прийти из чужих текстов,
поэтому в запросе она подаётся как данные. Сбой или мусор в ответе -> пустой список, вызывающий решает, что делать дальше.
"""
import json
import logging
from typing import Any, Callable

from contracts import LanguageModel
from domain import tasks as dom

logger = logging.getLogger("tasks")

TIMEOUT_S = 25.0
MAX_TOKENS = 400


def parse_steps(raw: str) -> list[dict]:
    """Шаги из ответа модели: JSON {"steps": [...]} (возможно в обрамлении текста) или список строк с маркерами."""
    if not raw:
        return []
    start, end = raw.find("{"), raw.rfind("}")
    items: Any = None
    if 0 <= start < end:
        try:
            data = json.loads(raw[start:end + 1])
            items = data.get("steps") if isinstance(data, dict) else None
        except ValueError:
            items = None
    if items is None:
        items = [ln.strip(" \t-*•0123456789.)") for ln in raw.splitlines()]
    return [s.to_dict() for s in dom.make_steps(items) if s.text]


class LlmPlanner:
    def __init__(self, llm: LanguageModel, prompt: Callable[[], dict], *, timeout: float = TIMEOUT_S):
        """prompt() -> {"system", "format"} (`prompts.planner`)."""
        self.llm, self.prompt, self.timeout = llm, prompt, timeout

    async def plan(self, goal: str, workspace_ctx: dict) -> list[dict]:
        p = self.prompt()
        ctx = json.dumps({k: v for k, v in (workspace_ctx or {}).items() if v}, ensure_ascii=False)[:800]
        user = f"Среда: {ctx}\nЦель (данные): {str(goal)[:dom.GOAL_MAX]}\n{p['format']}"
        try:
            raw = await self.llm.ask(p["system"], user, timeout=self.timeout, max_tokens=MAX_TOKENS, temperature=0.3, background=True)
        except Exception as e:
            logger.warning(f"Planner: {e}")
            return []
        return parse_steps(raw or "")
