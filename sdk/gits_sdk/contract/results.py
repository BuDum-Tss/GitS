"""Типизированные ответы ядра на запросы модуля (ТЗ 31, MC-3): то, что возвращают методы `ModuleClient`.

Поля повторяют `response` описаний в `messages.py`; лишние поля ответа (версия ядра новее) сохраняются в `raw`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TtsResult:
    """`core.tts`: звуковой файл, синтезированный модулем голоса."""
    path: str
    duration_ms: int

    @classmethod
    def from_wire(cls, v: dict) -> TtsResult:
        return cls(path=str(v["path"]), duration_ms=int(v["duration_ms"]))


@dataclass(frozen=True)
class ToolResult:
    """`core.tool.call`: текст, который вернул инструмент."""
    text: str

    @classmethod
    def from_wire(cls, v: dict) -> ToolResult:
        return cls(text=str(v.get("text", "")))


@dataclass(frozen=True)
class Persona:
    """`core.persona`: карточка активного персонажа и разделы persona.md."""
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    gender: str = ""
    language: str = ""
    world: str = ""
    voice: dict = field(default_factory=dict)
    card: str = ""
    sections: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, v: dict) -> Persona:
        return cls(id=str(v.get("id", "")), name=str(v.get("name", "")), aliases=tuple(v.get("aliases") or ()),
                   gender=str(v.get("gender") or ""), language=str(v.get("language") or ""), world=str(v.get("world") or ""),
                   voice=dict(v.get("voice") or {}), card=str(v.get("card") or ""), sections=dict(v.get("sections") or {}),
                   raw=dict(v))


@dataclass(frozen=True)
class LlmAnswer:
    """`core.llm.ask`: ответ локальной модели; `data` - разобранный JSON, если его просили и он разобрался."""
    text: str
    data: dict | None = None

    @classmethod
    def from_wire(cls, v: dict) -> LlmAnswer:
        data = v.get("data")
        return cls(text=str(v.get("text") or ""), data=data if isinstance(data, dict) else None)


@dataclass(frozen=True)
class JournalEvent:
    """Строка журнала из `core.events.tail`."""
    ts: float
    kind: str
    text: str
    data: dict = field(default_factory=dict)

    @classmethod
    def from_wire(cls, v: dict) -> JournalEvent:
        return cls(ts=float(v.get("ts", 0)), kind=str(v.get("kind", "")), text=str(v.get("text", "")),
                   data=dict(v.get("data") or {}))


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}
