"""Публичный контракт «ядро - модуль» (ТЗ 31): единый источник описаний и проверок.

    from gits_sdk import contract
    contract.validate("core.tts", {"text": "привет"})            # список проблем, пусто - верно
    contract.get("core.tts").response                             # поля ответа

Контракт читают SDK-клиент, `FakeCore`, ядро (`module_hub`, `core_requests`) и генератор `docs/MODULE_CONTRACT.md`.
"""
from __future__ import annotations

from typing import Any

from . import environment, messages
from .messages import (ALL_MESSAGES, CORE_REQUEST_MESSAGES, LOG_KINDS, LOG_READ_KINDS,
                       MODULE_REQUEST_MESSAGES, TOPIC_MESSAGES)
from .spec import (C2M, DEPRECATED, M2C, NOTE, PLANNED, REQUEST, STABLE, TOPIC, Field, Message, check_fields,
                   schema_of)
from .versions import CONTRACT_VERSION, PROTOCOL

REGISTRY: dict[str, Message] = {m.name: m for m in ALL_MESSAGES}
assert len(REGISTRY) == len(ALL_MESSAGES), "имена сообщений контракта повторяются"

CORE_REQUESTS = tuple(m.name for m in CORE_REQUEST_MESSAGES)           # модуль -> ядро, с ответом
MODULE_REQUESTS = tuple(m.name for m in MODULE_REQUEST_MESSAGES)       # ядро -> модуль, с ответом
TOPICS = tuple(m.name for m in TOPIC_MESSAGES)


def get(name: str) -> Message | None:
    return REGISTRY.get(name)


def validate(name: str, body: Any, **ctx: Any) -> list[str]:
    """Проблемы тела запроса, сообщения или темы `name`. Неизвестное имя: ["неизвестное сообщение"]."""
    msg = REGISTRY.get(name)
    if msg is None:
        return [f"неизвестное сообщение «{name}»"]
    return msg.validate(body, **ctx)


def validate_response(name: str, value: Any) -> list[str]:
    """Проблемы ответа (`result.value`) на запрос `name`."""
    msg = REGISTRY.get(name)
    if msg is None:
        return [f"неизвестное сообщение «{name}»"]
    return msg.validate_response(value)


def validate_topic(topic: str, data: Any) -> list[str]:
    msg = REGISTRY.get(topic)
    if msg is None or msg.kind != TOPIC:
        return [f"неизвестная тема «{topic}»"]
    return msg.validate(data)


def is_planned(name: str) -> bool:
    msg = REGISTRY.get(name)
    return msg is not None and msg.status == PLANNED


def planned_message(name: str) -> str:
    """Текст ошибки `unavailable` для запроса со статусом planned: с кодом и версией контракта."""
    msg = REGISTRY[name]
    return f"запрос «{name}» ещё не реализован (статус planned, {msg.planned_in or 'позже'}, контракт {CONTRACT_VERSION})"


__all__ = ["ALL_MESSAGES", "CONTRACT_VERSION", "CORE_REQUESTS", "Field", "LOG_KINDS", "LOG_READ_KINDS", "M2C", "C2M",
           "MODULE_REQUESTS", "Message", "PROTOCOL", "REGISTRY", "TOPICS", "environment", "get", "is_planned",
           "messages", "planned_message", "validate", "validate_response", "validate_topic", "check_fields",
           "schema_of", "DEPRECATED", "NOTE", "PLANNED", "REQUEST", "STABLE", "TOPIC"]
