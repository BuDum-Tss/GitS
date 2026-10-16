"""Услуги ядра для кода модуля, у которого нет под рукой `ModuleClient` (ТЗ 31, MC-4).

Модули больше не импортируют `events_log` и `fast_brain` ядра. Вместо этого:

    from gits_sdk import host
    host.log_nowait("hearing", "микрофон включён")                    # запись в журнал ядра (сообщение `log`)
    text = await host.llm_ask(system, user, max_tokens=100)           # локальная модель ядра (`core.llm.ask`)
    data = await host.llm_json(system, user, background=True)         # то же, ответ разобран как JSON
    rows = await host.tail(limit=8, kinds={"game"})                   # события журнала (`core.events.tail`)

Клиент процесса привязывается сам: им становится последний созданный `ModuleClient`. Без клиента (юнит-тесты логики) журнал
молчит, а модель и чтение журнала сообщают `ToolError(unavailable)`.
"""
from __future__ import annotations

from typing import Any

from .protocol import ToolError

_client: Any = None


def bind(client: Any) -> None:
    """Сделать клиент источником услуг процесса (вызывает `ModuleClient.__init__`)."""
    global _client
    _client = client


def unbind(client: Any = None) -> None:
    global _client
    if client is None or _client is client:
        _client = None


def current() -> Any:
    return _client


def log_nowait(kind: str, text: str = "", **data: Any) -> None:
    """Записать событие в журнал ядра. Не бросает исключений и не ждёт; без клиента ничего не делает."""
    c = _client
    if c is None:
        return
    try:
        c.log(kind, text, **data)
    except Exception:
        pass


log = log_nowait


def _need() -> Any:
    if _client is None:
        raise ToolError("нет связи с ядром: клиент модуля не создан", code="unavailable")
    return _client


async def llm_ask(system: str, user: str, *, timeout: float = 60.0, max_tokens: int = 40, temperature: float = 0.3,
                  background: bool = False, image_b64: str | None = None) -> str:
    """Один вызов локальной модели ядра; возвращает текст ответа. ToolError(unavailable/timeout) при сбое."""
    return (await _need().llm_ask(system, user, timeout=timeout, max_tokens=max_tokens, temperature=temperature,
                                  background=background, image_b64=image_b64)).text


async def llm_json(system: str, user: str, *, timeout: float = 60.0, max_tokens: int = 40, temperature: float = 0.3,
                   background: bool = False, image_b64: str | None = None) -> dict | None:
    """Как `llm_ask`, но ядро разбирает JSON из ответа (терпимо к небрежным моделям); None, если не разобралось."""
    ans = await _need().llm_ask(system, user, timeout=timeout, max_tokens=max_tokens, temperature=temperature,
                                background=background, image_b64=image_b64, as_json=True)
    return ans.data


async def tail(limit: int = 20, kinds: set | list | tuple | None = None, since: float = 0.0) -> list[dict]:
    """События журнала ядра (старые сверху). Ошибка связи: пустой список (журнал не должен ронять работу)."""
    c = _client
    if c is None:
        return []
    try:
        return [{"ts": e.ts, "kind": e.kind, "text": e.text, **({"data": e.data} if e.data else {})}
                for e in await c.events_tail(limit=limit, kinds=kinds, since=since)]
    except Exception:
        return []
