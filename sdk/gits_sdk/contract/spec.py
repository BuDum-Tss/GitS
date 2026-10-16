"""Описание элемента контракта (ТЗ 31, MC-1): поле, сообщение и общая проверка тела по полям.

Контракт один: эти описания читают SDK (клиент, `FakeCore`), ядро (`module_hub`, `core_requests`) и генератор документа
(`sdk/tools/gen_contract_doc.py`). Ни один из них не держит своих копий схем.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# направления
M2C = "module->core"
C2M = "core->module"

# виды
REQUEST = "request"       # ждёт ответа `result`
NOTE = "note"             # одностороннее сообщение
TOPIC = "topic"           # тема ядра (рассылка `topic`)

# статусы
STABLE = "stable"
PLANNED = "planned"
DEPRECATED = "deprecated"

TYPES = ("string", "integer", "number", "boolean", "array", "object", "any")


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class Field:
    """Поле тела. `max`/`min`: для строки длина, для массива число элементов, для числа значение."""
    name: str
    type: str
    required: bool = False
    doc: str = ""
    min: float | None = None
    max: float | None = None
    enum: tuple = ()
    nonempty: bool = False            # строка без пробельных краёв не пуста
    items: str | None = None          # тип элементов массива (string, object, ...)
    default: Any = None

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"поле {self.name}: неизвестный тип {self.type}")

    def schema(self) -> dict:
        """JSON-схема поля."""
        s: dict[str, Any] = {} if self.type == "any" else {"type": self.type}
        if self.doc:
            s["description"] = self.doc
        if self.enum:
            s["enum"] = list(self.enum)
        if self.type == "string":
            if self.max is not None:
                s["maxLength"] = int(self.max)
            if self.nonempty:
                s["minLength"] = 1
        elif self.type == "array":
            if self.items:
                s["items"] = {} if self.items == "any" else {"type": self.items}
            if self.max is not None:
                s["maxItems"] = int(self.max)
            if self.min is not None:
                s["minItems"] = int(self.min)
        elif self.type in ("integer", "number"):
            if self.min is not None:
                s["minimum"] = self.min
            if self.max is not None:
                s["maximum"] = self.max
        if self.default is not None:
            s["default"] = self.default
        return s


def check_value(f: Field, v: Any, where: str = "") -> list[str]:
    """Проблемы значения одного поля (пустой список: всё верно)."""
    p = where + f.name
    t = f.type
    if t == "string":
        if not isinstance(v, str):
            return [f"{p}: строка"]
        if f.nonempty and not v.strip():
            return [f"{p}: непустая строка"]
        if f.max is not None and len(v) > f.max:
            return [f"{p}: длиннее {int(f.max)} символов"]
    elif t == "integer" or t == "number":
        if not (_is_int(v) if t == "integer" else _is_num(v)):
            return [f"{p}: {'целое число' if t == 'integer' else 'число'}"]
        if f.min is not None and v < f.min:
            return [f"{p}: не меньше {f.min:g}"]
        if f.max is not None and v > f.max:
            return [f"{p}: не больше {f.max:g}"]
    elif t == "boolean":
        if not isinstance(v, bool):
            return [f"{p}: true или false"]
    elif t == "array":
        if not isinstance(v, list):
            return [f"{p}: список"]
        if f.max is not None and len(v) > f.max:
            return [f"{p}: не больше {int(f.max)} элементов"]
        if f.min is not None and len(v) < f.min:
            return [f"{p}: не меньше {int(f.min)} элементов"]
        if f.items == "string" and not all(isinstance(x, str) for x in v):
            return [f"{p}: список строк"]
        if f.items == "object" and not all(isinstance(x, dict) for x in v):
            return [f"{p}: список объектов"]
    elif t == "object":
        if not isinstance(v, dict):
            return [f"{p}: объект"]
    if f.enum and v not in f.enum:
        return [f"{p}: одно из {list(f.enum)}"]
    return []


def check_fields(fields: tuple[Field, ...], body: Any, *, what: str = "тело") -> list[str]:
    """Проверить объект по списку полей. Лишние поля допустимы (совместимость вперёд, MC-8)."""
    if not isinstance(body, dict):
        return [f"{what} должно быть объектом"]
    problems: list[str] = []
    for f in fields:
        if f.name not in body or (body[f.name] is None and f.type != "any"):
            if f.required:
                problems.append(f"{f.name}: обязательное поле")
            continue
        if body[f.name] is None:
            continue
        problems.extend(check_value(f, body[f.name]))
    return problems


def schema_of(fields: tuple[Field, ...], *, title: str = "", description: str = "") -> dict:
    """JSON-схема объекта по списку полей."""
    s: dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
                         "properties": {f.name: f.schema() for f in fields}, "additionalProperties": True}
    req = [f.name for f in fields if f.required]
    if req:
        s["required"] = req
    if title:
        s["title"] = title
    if description:
        s["description"] = description
    return s


@dataclass(frozen=True)
class Message:
    """Элемент контракта: запрос, сообщение или тема с направлением, полями, ошибками, статусом и примерами."""
    name: str
    direction: str
    kind: str
    doc: str
    since: str = "1.0"
    status: str = STABLE
    request: tuple[Field, ...] = ()
    response: tuple[Field, ...] = ()
    errors: tuple[str, ...] = ()
    # проверка тела, если полей мало (готовые validate_* протокола); получает тело и контекст
    check: Callable[..., list[str]] | None = None
    check_response: Callable[[Any], list[str]] | None = None
    valid: tuple[dict, ...] = ()
    invalid: tuple[Any, ...] = ()
    response_valid: tuple[Any, ...] = ()
    response_invalid: tuple[Any, ...] = ()
    max_bytes: int | None = None
    note: str = ""
    planned_in: str = ""            # для planned: в каком ТЗ появится
    deprecated_on: str = ""         # для deprecated: дата удаления
    extra: dict = field(default_factory=dict)

    def validate(self, body: Any, **ctx: Any) -> list[str]:
        if self.check is not None:
            return list(self.check(body, **ctx)) if ctx else list(self.check(body))
        return check_fields(self.request, body)

    def validate_response(self, value: Any) -> list[str]:
        if self.check_response is not None:
            return list(self.check_response(value))
        if not self.response:
            return [] if value in (None, {}) or isinstance(value, dict) else ["ответ: объект"]
        return check_fields(self.response, value, what="ответ")

    def request_schema(self) -> dict:
        return schema_of(self.request, title=self.name, description=self.doc)

    def response_schema(self) -> dict:
        return schema_of(self.response, title=self.name + " (ответ)")
