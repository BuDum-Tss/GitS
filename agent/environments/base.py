"""Окружения: единая модель «что ассистентка умеет и откуда это берётся».

Окружение (RimWorld, Minecraft, система ПК, соцсети…) — набор инструментов и событий.
Инструменты приходят из разных источников (Python-плагин, MCP-сервер, исходящий
вебхук), но реестр приводит их к одному виду: Tool. Дальше любая просьба идёт
одним путём: поиск подходящих инструментов -> «могу или нет» -> план -> исполнение
(router.py).

Уровни риска инструмента (политика исполнения):
    auto    - выполняется сразу (чтение, безопасные настройки самой ассистентки);
    ask     - перед выполнением вслух спрашивается разрешение;
    public  - публичное действие (пост в канал): спрашивается, только если включён
              флаг telegram_confirm (по умолчанию публикует сама).
"""
from dataclasses import dataclass, field
from typing import Awaitable, Callable

RISKS = ("auto", "ask", "public")


class ToolError(Exception):
    """Ожидаемый отказ инструмента (понятная причина, а не сбой кода)."""


@dataclass
class Tool:
    id: str                       # "social.telegram_post"
    env: str
    name: str
    description: str
    # упрощённая схема: имя -> {"type": string|integer|number|boolean|array|object,
    #                           "description": ..., "required": True, "enum": [...]}
    params: dict = field(default_factory=dict)
    risk: str = "ask"
    keywords: tuple = ()
    call: Callable[[dict], Awaitable[str]] | None = None
    # инструмент сам показывает точное содержимое и спрашивает разрешение (пост в канал):
    # для риска public маршрутизатор тогда не спрашивает второй раз
    self_confirming: bool = False
    # результат несёт информацию, которую нужно сообщить пользователю (описание экрана, ответ
    # Claude); у простых действий (переключить настройку) итог молчаливый: хватает «сейчас сделаю»
    informative: bool = False
    # (аргументы) -> причина или None: действие спрашивается ВСЕГДА, даже при авто-разрешении
    danger: Callable[[dict], str | None] | None = None
    # универсальный запасной инструмент (Claude-помощник): попадает к планировщику при любом отборе
    always: bool = False

    def signature(self) -> str:
        """Описание для подсказки планировщику."""
        parts = []
        for name, p in self.params.items():
            kind = p.get("type", "string")
            opt = "" if p.get("required", True) else ", необязательный"
            enum = f", одно из {p['enum']}" if p.get("enum") else ""
            desc = f" - {p['description']}" if p.get("description") else ""
            parts.append(f"{name} ({kind}{opt}{enum}){desc}")
        args = "; ".join(parts) if parts else "без параметров"
        return f"{self.id}: {self.description} Параметры: {args}. Риск: {self.risk}."


_TRUE, _FALSE = {"true", "1", "да", "yes", "on", "вкл"}, {"false", "0", "нет", "no", "off", "выкл"}


def validate_args(tool: Tool, args) -> tuple[dict, str | None]:
    """Привести аргументы к типам схемы. Возвращает (аргументы, ошибка или None)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {}, "аргументы должны быть объектом"
    unknown = set(args) - set(tool.params)
    if unknown:
        return {}, f"неизвестные параметры: {', '.join(sorted(unknown))}"
    clean = {}
    for name, p in tool.params.items():
        if name not in args or args[name] in (None, ""):
            if p.get("required", True):
                return {}, f"не хватает параметра «{name}»"
            continue
        v, kind = args[name], p.get("type", "string")
        try:
            if kind == "boolean":
                if isinstance(v, str):
                    low = v.strip().lower()
                    if low in _TRUE:
                        v = True
                    elif low in _FALSE:
                        v = False
                    else:
                        raise ValueError
                v = bool(v)
            elif kind == "integer":
                v = int(v)
            elif kind == "number":
                v = float(v)
            elif kind == "string":
                v = str(v)
        except (TypeError, ValueError):
            return {}, f"параметр «{name}» должен быть типа {kind}"
        if p.get("enum") and v not in p["enum"]:
            return {}, f"параметр «{name}» должен быть одним из {p['enum']}"
        clean[name] = v
    return clean, None


@dataclass
class Environment:
    id: str
    title: str
    description: str
    tools: list[Tool] = field(default_factory=list)
    enabled: bool = True
    module: str = "core"          # модуль режима (modes.py): вне его режима окружение выключено
    # () -> (доступно ли сейчас, пояснение)
    status: Callable[[], tuple[bool, str]] | None = None

    def state(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, "выключено"
        if self.status is None:
            return True, "доступно"
        try:
            return self.status()
        except Exception as e:
            return False, f"ошибка проверки: {e}"
