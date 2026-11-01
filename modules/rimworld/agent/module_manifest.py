"""Манифест модуля RimWorld и обработчики его инструментов (ТЗ 11, RW-8..RW-11, RW-14, RW-17).

Инструменты строятся автоматически из MCP-сервера `rimworld_mcp.py` (RW-10): список и схемы параметров берутся публичным API
FastMCP (`await mcp.list_tools()`), схема переводится в упрощённые `params` контракта (та же логика, что в ядре у прежнего
`environments/sources._schema_to_params`). Обработчик инструмента вызывает функцию MCP-сервера в потоке (функции синхронные).
Приказов игре нет: единый агент ядра сам действует инструментами узлов (`workspace_module.py`).
"""
import asyncio
import fnmatch
import inspect
import json
import logging
from typing import get_args, get_origin

from gits_sdk import ToolError
from pydantic import TypeAdapter, ValidationError

import gamenet
from core_link import EVENTS

logger = logging.getLogger("module_manifest")

MODULE_ID = "rimworld"
VERSION = "1.0.0"
DESCRIPTION = ("Игра-симулятор колонии RimWorld: рабочее место агента. Агент сам играет в колонию инструментами узлов (интерфейс игры: "
               "вкладки, действия, запросы) и получает события игры (набег, гибель, пожар, письмо).")
# как в прежнем GitS-манифесте rimworld.json: что не отдаётся планировщику ассистентки
EXCLUDE = ("game_*", "get_pending_events")
KEYWORDS = ["игра", "колония", "колонист", "римворлд", "rimworld"]
TOOL_TIMEOUT_S = 60
DESCRIPTION_MAX = 300
PARAM_DESCRIPTION_MAX = 420
RESULT_MAX = 2000               # как у прежнего MCP-источника ядра: длиннее ответ инструмента обрезался

SHARED_SETTINGS = ["streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted", "telegram_after_run",
                   "record_runs"]
SETTINGS_SCHEMA = {
    "auto_montage": {"type": "boolean", "title": "Отдавать монтаж забега модулю montage (если он подключён)",
                     "default": False},
    "max_record_hours": {"type": "number", "title": "Запись забега: после скольких часов переходить к следующему куску видео",
                         "default": 6, "min": 0.5, "max": 24},
    "stall_unpause_s": {"type": "number", "title": "Игра на паузе дольше этого (секунд) и пользователя нет за компьютером: "
                        "снять паузу (запись и автозапуск игры)", "default": 180, "min": 60, "max": 1800},
    "autostart_game": {"type": "boolean", "title": "Запускать игру самой, когда модуль в фокусе (по умолчанию выключено: "
                       "игра запускается по просьбе или инструментом launch_game)", "default": False},
    "observe_max_speed": {"type": "number", "title": "Наибольшая скорость игры в наблюдении, когда тихо (1-3)",
                          "default": 3, "min": 1, "max": 3},
    "manual_stall_hours": {"type": "number", "title": "Ручное управление: задача застряла, если не двигается дольше этого "
                           "(игровых часов)", "default": 2, "min": 0.5, "max": 24},
    "manual_confirm": {"type": "boolean", "title": "Ручное управление: только предлагать принудительную работу, не выполнять "
                       "без подтверждения", "default": False},
    "pause_while_thinking": {"type": "boolean", "title": "Ставить игру на паузу, пока Гладос думает над ходом (задержка модели не "
                             "стоит игрового времени, ролик связный)", "default": True},
    "narration": {"type": "string", "title": "Реплики Гладос: script (текст в тетрадь для ролика, озвучка потом; пока идёт запись) "
                  "или live (говорит вслух во время игры)", "default": "script", "enum": ["script", "live"]},
    "camera_agent": {"type": "boolean", "title": "Камерой управляет агент (выключено: камера у игрока, агент её не двигает). Главный переключатель - кнопка «CAM» у правого края экрана в игре; настройка передаётся ей при запуске игры и при изменении",
                     "default": True},
    "focus_mode": {"type": "string", "title": "Фокус агента: core - только запросы ядра (среду включает импульс: сообщение или "
                   "решение агента); manual - только кнопки на панели RimWorld; auto - старый режим, модуль сам берёт "
                   "фокус, когда игра запущена (в среду это не входит)",
                   "default": "core", "enum": ["auto", "core", "manual"]},
}
# среда агента (ТЗ 32, WS-4): в неё входят по сообщению пользователя, по самоимпульсу или по критическому сигналу
WORKSPACE = {"id": MODULE_ID, "title": "RimWorld",
             "purpose": "играть в свою колонию самой: осматриваться, строить, настраивать работы, защищать",
             "entry": ["user_message", "self_impulse", "critical"], "interruptible": True,
             "safe_points": ["игра на паузе", "тихо: нет угрозы и боя"], "signals_in": ["telegram.*"],
             "done_when": "забег закончен или игра закрыта",
             "autonomous": True, "work_interval_s": 120,
             "default_goal": "Развивать колонию: еда и жильё для колонистов, работы и режим дня, постройки, исследования, защита от угроз"}      # ТЗ 33: пока агент в среде и свободен, ядро раз в 120 с шлёт рабочий цикл
# что модуль шлёт ядру (RW-9): срочности берутся из core_link.EVENTS, чтобы не расходились
_EMIT_TEXT = {
    "rimworld.colonists": "Обновился список колонистов (служебное: отношения и граф памяти)",
    "rimworld.run_ended": "Забег закончен (колония погибла)",
    "rimworld.raid": "Набег, осада или другая угроза колонии",
    "rimworld.death": "Погиб колонист",
    "rimworld.fire": "В колонии начался пожар",
    "rimworld.letter": "Игровое письмо (событие в колонии, торговцы, гости, ссоры)",
    "rimworld.goal": "Цель игры достигнута или заброшена",
    "rimworld.run_video": "Видео забега готово (пути к записи, сценарию и клипам в data)",
    "rimworld.pawn_downed": "Колонист упал и лежит (ранен или без сознания)",
    "rimworld.enemy_seen": "На карте появился враг",
    "rimworld.enemy_down": "Враг убит или повержен",
    "rimworld.research_done": "Исследование завершено",
    "rimworld.resource_out": "Кончается запас (еда)",
    "rimworld.game_started": "Игра запущена (сигнал; в среду RimWorld агент входит только по своему импульсу или просьбе)",
}
# контракт 1.8: темы события (по ним узел сдвигает срочность), срочность вне фокуса, какие запросы смотреть после события
_EMIT_EXTRA = {
    "rimworld.raid": {"topics": ["threat"], "follow_up": ["colony_threats"]},
    "rimworld.death": {"topics": ["health", "social"], "follow_up": ["colony_colonists"]},
    "rimworld.fire": {"topics": ["threat"], "follow_up": ["colony_threats"]},
    "rimworld.letter": {"topics": ["social"], "urgency_unfocused": 1},
    "rimworld.goal": {"urgency_unfocused": 1},
    "rimworld.run_ended": {"follow_up": ["colony_overview"]},
    "rimworld.pawn_downed": {"topics": ["health", "threat"], "follow_up": ["colony_colonists"]},
    "rimworld.enemy_seen": {"topics": ["threat"], "follow_up": ["colony_threats"]},
    "rimworld.enemy_down": {"topics": ["threat"], "urgency_unfocused": 1},
    "rimworld.research_done": {"topics": ["economy"], "urgency_unfocused": 1},
    "rimworld.resource_out": {"topics": ["economy"], "follow_up": ["colony_resources"]},
}
EMITS = [{"kind": kind, "description": _EMIT_TEXT[kind], "default_urgency": urgency, **_EMIT_EXTRA.get(kind, {})}
         for kind, urgency in EVENTS.values()]


def schema_to_params(schema: dict, doc: str = "") -> dict:
    """JSON-схема параметров MCP -> упрощённые `params` контракта (ТЗ 01, 6.1). Для необязательных параметров с `anyOf`
    (`int | None`) берётся первый тип, кроме null."""
    required = set((schema or {}).get("required", []))
    out = {}
    for name, p in ((schema or {}).get("properties") or {}).items():
        out[name] = {"type": _param_type(p),
                     "description": str(p.get("description", p.get("title", "")))[:PARAM_DESCRIPTION_MAX],
                     "required": name in required}
        if p.get("enum"):
            out[name]["enum"] = p["enum"]
        if out[name]["type"] in ("array", "object") and not out[name]["description"]:
            out[name]["description"] = doc_tail(doc)         # формат вложенного значения в схеме не виден: берём пример из docstring
    return out


def doc_tail(doc: str) -> str:
    """Docstring без первой строки (там лежит пример формата сложных параметров), сжатый в одну строку."""
    lines = [x.strip() for x in (doc or "").strip().splitlines()]
    return " ".join(x for x in lines[1:] if x)[:PARAM_DESCRIPTION_MAX]


def _param_type(p: dict) -> str:
    kind = p.get("type")
    if isinstance(kind, str):
        return kind
    for option in p.get("anyOf") or []:
        if isinstance(option, dict) and isinstance(option.get("type"), str) and option["type"] != "null":
            return option["type"]
    return "string"


def first_line(text: str | None, fallback: str) -> str:
    """Описание инструмента: первая непустая строка docstring, до 300 символов."""
    for line in (text or "").strip().splitlines():
        if line.strip():
            return line.strip()[:DESCRIPTION_MAX]
    return fallback


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE)


async def mcp_tool_specs(mcp=None) -> list[dict]:
    """Инструменты MCP-сервера в виде записей манифеста (без `order`)."""
    if mcp is None:
        import rimworld_mcp
        mcp = rimworld_mcp.mcp
    tools = []
    for t in await mcp.list_tools():
        if _excluded(t.name):
            continue
        tools.append({"name": t.name, "description": first_line(t.description, t.name),
                      "params": schema_to_params(t.inputSchema, t.description), "risk": "auto", "availability": "profile",
                      "timeout_s": TOOL_TIMEOUT_S, "keywords": list(KEYWORDS)})
    return tools


def launch_spec() -> dict:
    """Инструмент «запустить игру» (ТЗ 11e, POL-12): только по явной просьбе, с объявлением. Обработчик в attach, не в MCP."""
    return {"name": "launch_game", "description": "Запустить игру RimWorld (если она не запущена). Пользователь слышит объявление "
                                                  "о запуске заранее; reason обязателен: зачем.",
            "params": {"reason": {"type": "string", "description": "зачем запускаем, одной фразой", "required": True}},
            "risk": "auto", "availability": "profile", "timeout_s": 120, "keywords": list(KEYWORDS)}


def build_manifest(tools: list[dict], nodes: list[dict] | None = None) -> dict:
    from game_panel import panel_spec
    import policies
    names = {t["name"] for t in tools}
    emits = json.loads(json.dumps(EMITS))
    for emit in emits:                                   # follow_up только на существующие инструменты (без них, как в тестах, пусто)
        if "follow_up" in emit:
            emit["follow_up"] = [n for n in emit["follow_up"] if n in names]
            if not emit["follow_up"]:
                del emit["follow_up"]
    return {"id": MODULE_ID, "title": "RimWorld", "description": DESCRIPTION, "version": VERSION,
            "capabilities": ["game"], "subscribe": ["presence", "settings", "profile", "module"],
            "shared_settings": list(SHARED_SETTINGS), "settings_schema": json.loads(json.dumps(SETTINGS_SCHEMA)),
            "tools": [launch_spec(), *tools], "emits": emits, "graph": True, "state": True,
            "panel": panel_spec(),                       # пространство в дашборде (TZ 15, TAB-5, TAB-9)
            # политики (ТЗ 18, FOC-1; ТЗ 11e, POL-11/13): минимум одна с focus background (афк), она по умолчанию;
            # переходы в формате ядра (из фоновой только запрашиваемые, см. policies.Registry.transitions)
            "policies": policies.get().manifest(), "default_policy": policies.get().default,
            "workspace": dict(WORKSPACE), **({"nodes": nodes} if nodes else {})}


async def build(mcp=None) -> dict:
    """Готовый манифест модуля: `order`, `launch_game`, навигация, действия узлов и запросы по дереву рабочего места
    (`workspace_module`), дерево узлов `nodes`."""
    import workspace_module
    tools = await workspace_module.tool_specs(mcp)
    return build_manifest(tools, workspace_module.manifest_nodes({t["name"] for t in tools}))


# ── обработчики ────────────────────────────────────────────────────────────────────────────────

def _coerce(fn, name: str, args: dict) -> dict:
    """Аргументы к типам функции (как это делал бы FastMCP): "5" -> 5, JSON-строка -> список. Лишние ключи отбрасываются."""
    params = inspect.signature(fn).parameters
    out = {}
    for key, value in (args or {}).items():
        param = params.get(key)
        if param is None:
            continue
        annotation = param.annotation
        if annotation is inspect.Parameter.empty:
            out[key] = value
            continue
        if isinstance(value, str) and _wants_json(annotation):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        try:
            out[key] = TypeAdapter(annotation).validate_python(value)
        except ValidationError as e:
            raise ToolError(f"{name}: параметр «{key}» неверен: {e.errors()[0]['msg']}", code="bad_request") from e
    return out


def _wants_json(annotation) -> bool:
    """Параметр списка или объекта, который планировщик мог передать JSON-строкой."""
    types = [annotation, *get_args(annotation)]
    return str not in types and any((get_origin(t) or t) in (list, dict) for t in types)


def tool_handler(name: str, module=None):
    """Обработчик одного инструмента MCP. Функция ищется в момент вызова (подмена в тестах)."""
    async def handler(args: dict):
        mod = module
        if mod is None:
            import rimworld_mcp as mod
        fn = getattr(mod, name, None)
        if not callable(fn):
            raise ToolError(f"инструмента «{name}» в MCP-сервере нет", code="unknown")
        if not await asyncio.to_thread(gamenet.port_open):
            # как прежнее требование requires_port у окружения в ядре: без игры инструменты недоступны
            raise ToolError("игра сейчас не запущена (порт мода закрыт)", code="unavailable")
        kwargs = _coerce(fn, name, args)
        result = await asyncio.to_thread(fn, **kwargs)
        return str(result if result is not None else "")[:RESULT_MAX]
    return handler


def attach(client, tools: list[dict], graph, module=None, launch=None, focus=None, node_state=None) -> None:
    """Зарегистрировать у клиента обработчики всех инструментов манифеста, `launch_game`, `graph.get` и запросы фокуса.
    graph: () -> dict (game_api.graph_data);
    launch: async (reason) -> str (запуск игры с объявлением, ТЗ 11e POL-12); focus: объект с async activate(body) и
    release(body) для запросов ядра policy.activate и focus.release (ТЗ 11e POL-14)."""
    import workspace_module as wm
    state = node_state if node_state is not None else wm.NodeState()
    nav = wm.make_nav_handlers(state, module)
    for tool in tools:
        name = tool["name"]
        if name in nav:
            client.tool(name)(nav[name])
        elif name in wm.OLD_BY_NAME:
            client.tool(name)(wm.make_tool_handler(name, tool.get("node") or "", state, module))
        else:                                           # прежний плоский манифест (тесты, совместимость)
            client.tool(name)(tool_handler(name, module))

    @client.tool("launch_game")
    async def _launch(args):                                             # POL-12: только по просьбе, с объявлением
        reason = str((args or {}).get("reason") or "").strip()
        if not reason:
            raise ToolError("launch_game: нужна причина (reason): зачем запускаем игру", code="bad_request")
        if launch is None:
            raise ToolError("запуск игры в этом процессе не подключён", code="unavailable")
        return {"result": await launch(reason)}

    @client.on_request("graph.get")
    async def _graph(body):                                              # RW-14a
        return graph()

    if focus is not None:
        # ТЗ 32, WS-4: новые имена запросов среды; прежние (policy.activate, focus.release, focus.resume) SDK ведёт к тем же
        # обработчикам как алиасы
        @client.on_request("workspace.enter")
        async def _activate(body):                                       # POL-14
            return await focus.activate(body or {})

        @client.on_request("workspace.exit")
        async def _release(body):                                        # POL-14
            return await focus.release(body or {})

        @client.on_request("workspace.resume")
        async def _resume(body):                                         # ТЗ 18, FOC-4: возобновление по checkpoint
            return await focus.resume(body or {})

        @client.panel_action("take_focus")                               # правка fix/focus-bootstrap: кнопки панели
        async def _take_focus(args):
            return await focus.panel_take_focus(args or {})

        @client.panel_action("release_focus")
        async def _release_focus(args):
            return await focus.panel_release_focus(args or {})
