"""Быстрая модель: один вызов `_ask` и потоковый `_ask_stream` (оба идут в Claude через `claude_llm`).

Отдельного локального слоя нет: реплика по факту, зрение, размышление и запросы модулей (`core.llm.ask`) вызывают здесь
Claude без инструментов (модель `fast_claude_model`). Разговор с владельцем ведёт единый агент (`app/resident.py`). Теги
стикеров ставит сама модель в тексте ответа (`domain/stickers.py`).
"""
import json
import logging
import re
import time

import flow_trace

logger = logging.getLogger("fast_brain")


def _json_of(txt: str) -> dict | None:
    """Достать JSON из ответа модели. Небольшие модели иногда пишут текстовое
    поле без кавычек ("text": Привет, мир, "x": ...), поэтому при сбое
    строгого разбора значения чинятся: что не JSON, то считается строкой."""
    a, b = txt.find("{"), txt.rfind("}")
    body = txt[a:b + 1]
    try:
        return json.loads(body)
    except ValueError:
        pass
    keys = list(re.finditer(r'"(\w+)"\s*:\s*', body))
    if not keys:
        return None
    out = {}
    for i, m in enumerate(keys):
        end = keys[i + 1].start() if i + 1 < len(keys) else len(body)
        raw = body[m.end():end].strip()
        if i + 1 == len(keys):
            raw = raw.rstrip("}").strip()
        raw = raw.rstrip(",").strip()
        try:
            out[m.group(1)] = json.loads(raw)
        except ValueError:
            out[m.group(1)] = raw.strip('"').strip()
    return out


async def _ask(system: str, user: str, timeout: float, max_tokens: int = 40,
               image_b64: str | None = None, temperature: float = 0.3,
               background: bool = False, parallel: bool = False) -> str:
    """Один вызов Claude без инструментов. `max_tokens`, `temperature`, `background` и `parallel` остались в сигнатуре для
    вызывающих: Claude их не использует."""
    import claude_llm
    t0 = time.perf_counter()
    out = await claude_llm.ask(system, user, max(timeout, 30.0), image_b64=image_b64)
    flow_trace.step(None, "gemma", "planner", "gemma", "llm", ms=(time.perf_counter() - t0) * 1000, size=len(system) + len(str(user)),
                    label="claude")
    return out


async def _ask_stream(system: str, user: str, timeout: float, max_tokens: int = 140, temperature: float = 0.7):
    """Ответ потоком кусков текста (асинхронный генератор). Закрытие генератора обрывает запрос."""
    import claude_llm
    async for piece in claude_llm.ask_stream(system, user, max(timeout, 30.0)):
        yield piece
