"""Рабочее место RimWorld для ядра (контракт 1.8, `GitS/docs/WORKSPACE_MODULES.md`).

Манифест строится по дереву `workspace_tree.py`: ядро видит узлы (`nodes`), действия узла (`tools[].node`) и запросы (всегда),
а не плоский список из ста инструментов. Старые функции MCP-сервера остаются исполнителями: игровой агент модуля пока пользуется
ими напрямую, а здесь новое имя инструмента ведёт к старой функции.

Имена: действие узла `<узел>_<действие>` (`architect_orders_mine`), запрос `<путь>` (`colony_resources`), навигация `open`,
`back`, `where`, время `pause`, `resume`, `set_speed` (глобальные, как кнопки внизу экрана).

Текущий узел хранит `NodeState`. Действие вне своего узла отклоняется с подсказкой, какой узел открыть (WS-10). Навигация
повторяет игрока: `open("architect/zone")` открывает вкладку «Архитектор» и категорию, `open("pawn", id=...)` выделяет колониста,
`back()` поднимается на уровень выше, а у корня закрывает окно.
"""
import asyncio
import json
import logging
from typing import Callable

from gits_sdk import ToolError

import workspace_tree as wt

logger = logging.getLogger("workspace_module")

ROOT = "root"
NAV = ("open", "back", "where")
TOOL_TIMEOUT_S = 60
KEYWORDS = ["игра", "колония", "колонист", "римворлд", "rimworld"]
# вкладка игры (defName) -> имя для open_game_tab
TAB_NAMES = {"Architect": "architect", "Work": "work", "Schedule": "schedule", "Assign": "assign", "Research": "research",
             "Animals": "animals"}
# подузел Архитектора -> категория меню
ARCHITECT_CATEGORIES = {"architect/orders": "Orders", "architect/zone": "Zone", "architect/structure": "Structure"}


def tool_name(kind: str, path: str, new: str) -> str:
    """Имя инструмента в манифесте: см. заголовок модуля."""
    if path == ROOT:
        return new
    prefix = path.replace("/", "_")
    if kind == "query":
        return prefix if new == path.rpartition("/")[2] else f"{prefix}_{new}"
    return f"{prefix}_{new}"


def _specs_table() -> list[tuple[str, str, str, str, str]]:
    """(старое имя, вид, путь, новое имя инструмента, узел) для всего, что ядро получит из старых функций MCP-сервера."""
    out = []
    for old, (kind, path, new) in wt.TOOLS.items():
        if kind not in ("action", "query", "skill", "nav") or not new:
            continue
        if kind in ("action", "skill") and (path != ROOT):
            node = path
        else:
            node = ""                         # запросы, навигация и действия корня глобальны
        out.append((old, kind, path, tool_name(kind, path, new), node))
    return out


OLD_BY_NAME: dict[str, str] = {name: old for old, _k, _p, name, _n in _specs_table() if old not in ("open_game_tab", "ui_state")}


def node_titles() -> dict[str, str]:
    return {n.path: n.title for n in wt.NODES}


# ── узлы и панели в манифесте ─────────────────────────────────────────────────

def manifest_nodes(tool_names: set[str]) -> list[dict]:
    """Узлы для манифеста. Запросы панели берутся из дерева (путь запроса -> имя инструмента); отсутствующие пропускаются."""
    by_query = {path: name for _old, kind, path, name, _n in _specs_table() if kind == "query"}
    nodes = []
    for n in wt.NODES:
        item: dict = {"path": n.path, "title": n.title}
        if n.tab:
            item["tab"] = n.tab
        ctx = [{"tool": by_query[q], "mode": "on_change", "max_chars": 400} for q in n.context
               if by_query.get(q) in tool_names]
        if ctx:
            item["context"] = ctx
        if n.watch:
            item["watch"] = {topic: shift for topic, shift in n.watch}
        nodes.append(item)
    return nodes


# ── текущий узел ──────────────────────────────────────────────────────────────

class NodeState:
    def __init__(self, on_change: Callable[[], None] | None = None):
        self.path = ROOT
        self.on_change = on_change
        self._by_path = {n.path: n for n in wt.NODES}

    def known(self, path: str) -> bool:
        return path in self._by_path

    def children(self, path: str) -> list[str]:
        """Дочерние узлы: у корня верхний уровень, у остальных узлы с этим родителем."""
        parent = "" if path == ROOT else path
        return sorted(p for p in self._by_path if p != ROOT and p.rpartition("/")[0] == parent)

    def set(self, path: str) -> None:
        if path != self.path:
            self.path = path
            if self.on_change is not None:
                try:
                    self.on_change()
                except Exception as e:                 # сбой отправки состояния не должен ломать навигацию
                    logger.debug(f"on_change: {e}")

    def require(self, node: str, tool: str) -> None:
        """WS-10: действие вне своего узла отклоняется и называет нужный узел."""
        if node and node != self.path:
            raise ToolError(f"«{tool}» делается в узле «{node}», а сейчас открыт «{self.path}». "
                            f"Сначала open(\"{node}\"), затем это действие.", code="bad_request")


# ── обработчики ───────────────────────────────────────────────────────────────

def _call(module, name: str):
    fn = getattr(module, name, None)
    if not callable(fn):
        raise ToolError(f"функции «{name}» в MCP-сервере нет", code="unknown")
    return fn


async def _run(module, name: str, **kwargs) -> str:
    return str(await asyncio.to_thread(_call(module, name), **kwargs) or "")


def make_nav_handlers(state: NodeState, module=None) -> dict[str, Callable]:
    """open, back, where. `module` - rimworld_mcp (подменяется в тестах)."""

    def mcp():
        if module is not None:
            return module
        import rimworld_mcp
        return rimworld_mcp

    async def open_node(args: dict):
        path = str((args or {}).get("path") or "").strip().strip("/")
        target = (args or {}).get("id")
        if not path:
            return {"node": state.path, "children": state.children(state.path),
                    "hint": "open(path) открывает узел; path из списка children"}
        if not state.known(path):
            raise ToolError(f"узла «{path}» нет. Есть: {', '.join(sorted(n.path for n in wt.NODES))}", code="bad_request")
        node = next(n for n in wt.NODES if n.path == path)
        m = mcp()
        if path.startswith("pawn") and not target and not state.path.startswith("pawn"):
            raise ToolError("колонист не выбран: open(\"pawn\", id=<id колониста>)", code="bad_request")
        if target and path.startswith("pawn"):
            reply = await _run(m, "select_target", pawn_id=str(target))
            if reply.startswith(("ERROR", "WARNING")):
                raise ToolError(reply, code="unavailable")
        if node.tab and node.tab in TAB_NAMES:
            reply = await _run(m, "open_game_tab", tab=TAB_NAMES[node.tab])
            if reply.startswith("ERROR"):
                raise ToolError(reply, code="unavailable")
        elif path == ROOT:
            await _run(m, "open_game_tab", tab="close")
        category = ARCHITECT_CATEGORIES.get(path)
        if category:
            reply = await asyncio.to_thread(m._query, {"type": "ui_category", "id": category})
            if str(reply).startswith(("ERROR", "WARNING")):
                raise ToolError(str(reply), code="unavailable")
        state.set(path)
        return {"node": path, "title": node.title, "children": state.children(path)}

    async def back(args: dict):
        parent = state.path.rpartition("/")[0] or ROOT
        if state.path == ROOT:
            return {"node": ROOT, "note": "уже на главном экране"}
        return await open_node({"path": parent})

    async def where(args: dict):
        text = await _run(mcp(), "ui_state")
        here = [name for _o, kind, _p, name, node in _specs_table() if node == state.path and kind in ("action", "skill")]
        try:
            ui = json.loads(text)
        except ValueError:
            ui = text
        return {"node": state.path, "title": node_titles().get(state.path, state.path), "actions": here,
                "children": state.children(state.path), "ui": ui}

    return {"open": open_node, "back": back, "where": where}


def make_tool_handler(name: str, node: str, state: NodeState, module=None) -> Callable:
    """Обработчик действия узла или запроса: проверка узла, вызов старой функции MCP-сервера."""
    from module_manifest import tool_handler           # аргументы к типам функции, поток, ответ строкой
    old = OLD_BY_NAME[name]
    inner = tool_handler(old, module)

    async def handler(args: dict):
        state.require(node, name)
        return await inner(args)
    return handler


# ── спецификации инструментов для манифеста ───────────────────────────────────

NAV_SPECS = {
    "open": ("Открыть узел рабочего места, как игрок открывает вкладку: open(\"architect/zone\"), open(\"work\"), "
             "open(\"pawn\", id=колонист). Без path показывает дочерние узлы. Действия узла доступны, пока он открыт.",
             {"path": {"type": "string", "description": "путь узла, например architect/zone, work, schedule, pawn/orders",
                       "required": False},
              "id": {"type": "string", "description": "id колониста для узлов pawn/*", "required": False}}),
    "back": ("Подняться на уровень выше (как закрыть вкладку); с корня ничего не делает.", {}),
    "where": ("Где сейчас рабочее место: открытый узел, его действия и дочерние узлы, состояние интерфейса игры текстом "
              "(вкладка, категория, выделение).", {}),
}


def navigation_specs() -> list[dict]:
    return [{"name": name, "description": text, "params": params, "risk": "auto", "availability": "profile",
             "timeout_s": TOOL_TIMEOUT_S, "keywords": list(KEYWORDS)} for name, (text, params) in NAV_SPECS.items()]


async def tool_specs(mcp=None) -> list[dict]:
    """Спецификации инструментов манифеста: навигация, действия узлов, запросы. Описание и параметры берутся у старых функций."""
    from module_manifest import first_line, schema_to_params
    if mcp is None:
        import rimworld_mcp
        mcp = rimworld_mcp.mcp
    old_specs = {t.name: t for t in await mcp.list_tools()}
    out = navigation_specs()
    for old, kind, path, name, node in _specs_table():
        if old in ("open_game_tab", "ui_state") or old not in old_specs:
            continue
        t = old_specs[old]
        spec = {"name": name, "description": first_line(t.description, name),
                "params": schema_to_params(t.inputSchema, t.description), "risk": "auto", "availability": "profile",
                "timeout_s": TOOL_TIMEOUT_S, "keywords": list(KEYWORDS)}
        if node:
            spec["node"] = node
        out.append(spec)
    return out
