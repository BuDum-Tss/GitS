"""Мостик игровых инструментов к ядру ГЛаДОС (ТЗ 11, RW-16): управление ПК по экрану и поиск в интернете.

MCP-сервер игры (`rimworld_mcp.py`) работает отдельным процессом под Claude и до шины модулей не достаёт, а в процессе модуля те
же функции вызывает обработчик инструмента. Поэтому и там и там запрос идёт одним путём: HTTP на собственный API модуля
(`game_api`, порт 8767, только localhost), а тот уже пересылает его ядру запросом `core.tool.call` или `core.web_search`.
Прежние адреса ядра (`/api/tool`, `/api/web_search` на :8766) удалены вместе со старым каналом агента игры.
"""
import logging
import os

import httpx

logger = logging.getLogger("core_bridge")

DEFAULT_PORT = 8767
TOOL_TIMEOUT_S = 190              # ядро само ограничивает шаг и может ждать разрешения пользователя
SEARCH_TIMEOUT_S = 170
# Запасной уровень: что игре разрешено просить у ядра. Идентификаторы инструментов ядра; после переезда управления ПК в модуль
# desktop (ТЗ 14) здесь поменяются имена (desktop.*).
PC_TOOLS = frozenset({"system.read_screen", "system.click_text", "system.operate_screen", "system.press_key",
                      "system.type_text", "system.scroll", "system.list_windows", "system.focus_window"})


def api_port() -> int:
    """Порт API модуля: RIMWORLD_MODULE_PORT, запасной RIMWORLD_AGENT_PORT (прежнее имя, читается одну версию), иначе 8767."""
    new, old = os.getenv("RIMWORLD_MODULE_PORT"), os.getenv("RIMWORLD_AGENT_PORT")
    if new:
        return int(new)
    if old:
        logger.warning("Переменная RIMWORLD_AGENT_PORT устарела: переименуй в RIMWORLD_MODULE_PORT")
        return int(old)
    return DEFAULT_PORT


def _base() -> str:
    return f"http://127.0.0.1:{api_port()}"


def tool(tool_id: str, args: dict) -> str:
    """Вызвать инструмент ядра. Возвращает его ответ или строку `ERROR: ...` (как остальные инструменты MCP-сервера)."""
    try:
        r = httpx.post(f"{_base()}/api/core/tool", json={"tool": tool_id, "args": args}, timeout=TOOL_TIMEOUT_S + 10)
        data = r.json()
        return data.get("result", "") if data.get("ok") else "ERROR: " + str(data.get("result") or data.get("error"))
    except Exception as e:
        return f"ERROR: ядро недоступно: {e}"


def web_search(query: str) -> str:
    """Поиск в интернете через ядро. Возвращает выжимку или строку `ERROR: ...`."""
    try:
        r = httpx.post(f"{_base()}/api/core/web_search", json={"query": query}, timeout=SEARCH_TIMEOUT_S + 10)
        r.raise_for_status()
        return r.json().get("answer") or "Ничего не нашла."
    except Exception as e:
        return f"ERROR: поиск недоступен (ядро не подключено?): {e}"
