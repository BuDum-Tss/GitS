"""HTTP-API модуля RimWorld (порт 8767, только localhost): оверлей для стрима, состояние игры, пульт отметок оценок.

С ТЗ 11 ядро ГЛаДОС получает состояние (`state`), приказы (инструмент `order`) и подграф (`graph.get`) по шине модулей, а не отсюда.
Здесь остаются: оверлей OBS (/overlay, /api/state), /api/game и /api/graph (те же данные, для оверлея и отладки), /api/appraisal_label (дашборд ядра пересылает отметки оценок событий) и два узких мостика для MCP-сервера игры,
который работает отдельным процессом под Claude и до шины не достаёт: /api/core/tool и /api/core/web_search
(запасной уровень «управление ПК по экрану» и поиск в интернете через ядро, см. core_bridge.py).
"""
import asyncio
import json
import logging

from envcompat import env
from pathlib import Path

import httpx
from aiohttp import web

import core_bridge
import gamenet
import settings

logger = logging.getLogger("game_api")

_RIMWORLD_BASE = "http://localhost:8765"
_MEMORY_DIR: Path = Path("./memory")
_CORE = None           # ModuleClient: запросы к ядру (core.tool.call, core.web_search) для мостика MCP-сервера
_STATIC = Path(__file__).parent / "static"


async def _game_up() -> bool:
    """Быстрая проверка порта игры: без неё запрос к выключенной игре висит секунды."""
    return await asyncio.to_thread(gamenet.port_open)


async def _rw_state() -> dict:
    if not await _game_up():
        return {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{_RIMWORLD_BASE}/state")
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def _load_memory() -> dict:
    result = {
        "last_decision": None,
        "recent_decisions": [],
        "pawn_fates": {},
        "colony_name": None,
    }
    run_file = _MEMORY_DIR / "current_run.json"
    if run_file.exists():
        try:
            run = json.loads(run_file.read_text(encoding="utf-8"))
            result["colony_name"] = run.get("colony_name")
            decisions = run.get("key_decisions", [])
            result["recent_decisions"] = decisions
            if decisions:
                last = decisions[-1]
                result["last_decision"] = {
                    "tick": last.get("tick"),
                    "commentary": last.get("directive", ""),
                    "actions": last.get("actions", []),
                }
            result["pawn_fates"] = run.get("pawn_fates", {})
        except Exception:
            pass
    return result


def _active_goals() -> list[dict]:
    goals = _read_json("goals.json", [])
    return [{"title": g.get("title"), "category": g.get("category"), "progress": g.get("progress_pct", 0),
             "criteria": g.get("success_criteria", "")} for g in goals
            if g.get("status") == "active" and g.get("category") != "ambition"][-6:]


def _task_tree() -> list[dict]:
    import tasks_tree
    return tasks_tree.build(_read_json("tasks.json", []))


def _ambition() -> dict | None:
    found = [g for g in _read_json("goals.json", []) if g.get("category") == "ambition" and g.get("status") == "active"]
    if not found:
        return None
    g = found[-1]
    return {"title": g.get("title"), "criteria": g.get("success_criteria", ""), "progress": g.get("progress_pct", 0),
            "notes": g.get("notes", "")}


def _read_json(name: str, default):
    try:
        return json.loads((_MEMORY_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _active_goals() -> list[dict]:
    goals = _read_json("goals.json", [])
    return [{"title": g.get("title"), "category": g.get("category"), "progress": g.get("progress_pct", 0),
             "criteria": g.get("success_criteria", "")} for g in goals
            if g.get("status") == "active" and g.get("category") != "ambition"][-6:]


def _task_tree() -> list[dict]:
    import tasks_tree
    return tasks_tree.build(_read_json("tasks.json", []))


def _ambition() -> dict | None:
    found = [g for g in _read_json("goals.json", []) if g.get("category") == "ambition" and g.get("status") == "active"]
    if not found:
        return None
    g = found[-1]
    return {"title": g.get("title"), "criteria": g.get("success_criteria", ""), "progress": g.get("progress_pct", 0),
            "notes": g.get("notes", "")}


def _is_local_json(request: web.Request) -> bool:
    """Только JSON и только со своей же страницы: браузерный cross-site запрос так не отправить."""
    if request.content_type != "application/json":
        return False
    origin = request.headers.get("Origin")
    return not (origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")))


async def game_snapshot() -> dict:
    """Игровая часть состояния в том виде, в каком её рисует дашборд ассистента."""
    import agent_mode
    rw = await _rw_state()
    mem = _load_memory()
    colony = rw.get("colony", {})
    connected = bool(rw) and "tick" in rw and rw.get("tick") is not None
    pawns = []
    for p in colony.get("pawns", []):
        mood = p.get("mood_pct", 0.5)
        mood_label = "happy" if mood >= 0.7 else "neutral" if mood >= 0.5 else "sad" if mood >= 0.3 else "breaking"
        conditions = p.get("conditions", [])
        job = p.get("current_job", "idle")
        if p.get("is_drafted"):
            job = "DRAFTED — " + job
        pawns.append({
            "id": p.get("id", ""), "name": p.get("name", "?"), "age": p.get("age"),
            "job": f"{job}  {', '.join(conditions)}".strip(),
            "mood_label": f"{mood_label} {int(mood * 100)}%",
            "health_pct": p.get("health_pct", 1.0), "dead": False,
            "work_priorities": p.get("work_priorities") or {},
            "schedule": p.get("schedule") or [],
        })
    research_raw = colony.get("research", {})
    return {
        "connected": connected,
        "colony_name": mem["colony_name"] or colony.get("name"),
        "tick": rw.get("tick"),
        "pawns": pawns,
        "manual_priorities": rw.get("manual_priorities"),
        "resources": colony.get("resources", {}),
        "research": {"current": research_raw.get("current"), "progress": research_raw.get("progress", 0.0)},
        "game_decision": mem["last_decision"] if connected else None,
        "recent_decisions": mem["recent_decisions"],
        "ambition": _ambition(),
        "goals": _active_goals(),
        "tasks": _task_tree(),
        "mode": agent_mode.get(),
        "pawn_fates": mem["pawn_fates"],
        "appraisals": _appraisals(),
    }


async def handle_api_game(request: web.Request) -> web.Response:
    return web.json_response(await game_snapshot())


def graph_data() -> dict:
    """Подграф модуля RimWorld для дашборда ядра (режимы агента и переходы). Его же отдаёт обработчик `graph.get` (RW-14a)."""
    import agent_mode
    return agent_mode.graph()


async def handle_api_graph(request: web.Request) -> web.Response:
    return web.json_response(graph_data())


def _appraisals() -> list:
    """Последние оценки событий моделью решений для дашборда (с итогом и отметкой игрока)."""
    try:
        import appraisal_log
        return appraisal_log.recent(14)
    except Exception:
        return []


async def handle_api_appraisal_label(request: web.Request) -> web.Response:
    """Отметка игрока: {"id": "...", "ok": true/false, "correct_kind": "combat" (если неверно), "note": ""}."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    import appraisal_log
    ok = appraisal_log.label(str(body.get("id") or ""), bool(body.get("ok")), str(body.get("correct_kind") or ""), str(body.get("note") or ""))
    return web.json_response({"saved": ok, "stats": appraisal_log.accuracy()})


async def handle_core_tool(request: web.Request) -> web.Response:
    """Мостик MCP-сервера игры к ядру: {"tool": "system.click_text", "args": {...}} -> {"ok": bool, "result": "..."}.
    Разрешение у пользователя спрашивает ядро, как для любого действия на ПК. Допустимы только инструменты управления экраном."""
    if not _is_local_json(request) or _CORE is None:
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    tool_id = str(body.get("tool") or "")
    if tool_id not in core_bridge.PC_TOOLS:
        return web.json_response({"ok": False, "result": "этот инструмент агентам игр недоступен"}, status=403)
    try:
        value = await _CORE.request("core.tool.call", {"tool": tool_id, "args": body.get("args") or {}},
                                    timeout=core_bridge.TOOL_TIMEOUT_S)
        return web.json_response({"ok": True, "result": str((value or {}).get("text") or "")})
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "result": "ядро не ответило вовремя"})
    except Exception as e:
        return web.json_response({"ok": False, "result": getattr(e, "message", None) or str(e)})


async def handle_core_web_search(request: web.Request) -> web.Response:
    """Мостик MCP-сервера игры к поиску в интернете ядра: {"query": "..."} -> {"answer": "..."}."""
    if not _is_local_json(request) or _CORE is None:
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    try:
        value = await _CORE.request("core.web_search", {"query": str(body.get("query") or "")},
                                    timeout=core_bridge.SEARCH_TIMEOUT_S)
        return web.json_response({"answer": str((value or {}).get("text") or "")})
    except asyncio.TimeoutError:
        return web.json_response({"answer": "", "error": "ядро не ответило вовремя"}, status=504)
    except Exception as e:
        return web.json_response({"answer": "", "error": getattr(e, "message", None) or str(e)}, status=502)


async def handle_overlay(request: web.Request) -> web.Response:
    """Оверлей для стрима (источник «Браузер» в OBS): режим, главная цель и задачи крупно, фон прозрачный."""
    try:
        html = (_STATIC / "overlay.html").read_text(encoding="utf-8")
    except OSError:
        return web.Response(text="overlay.html не найден", status=500)
    return web.Response(text=html, content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/overlay", handle_overlay)
    app.router.add_get("/api/game", handle_api_game)
    app.router.add_get("/api/state", handle_api_game)           # оверлей читает отсюда
    app.router.add_get("/api/graph", handle_api_graph)
    app.router.add_post("/api/appraisal_label", handle_api_appraisal_label)
    app.router.add_post("/api/core/tool", handle_core_tool)
    app.router.add_post("/api/core/web_search", handle_core_web_search)
    return app


async def start(memory_dir: str, rimworld_url: str = "http://localhost:8765", port: int | None = None, core=None):
    """Поднять HTTP-API. Возвращает AppRunner (его закрывают при остановке модуля). core - ModuleClient для мостиков к ядру."""
    global _MEMORY_DIR, _RIMWORLD_BASE, _CORE
    _MEMORY_DIR = Path(memory_dir)
    _RIMWORLD_BASE = rimworld_url
    _CORE = core
    settings.init(memory_dir)
    runner = web.AppRunner(build_app())
    await runner.setup()
    port = port if port is not None else core_bridge.api_port()
    site = web.TCPSite(runner, env("GITS_DASHBOARD_HOST", "127.0.0.1"), port)
    await site.start()
    logger.info(f"API модуля RimWorld: http://localhost:{port} (оверлей: /overlay)")
    return runner
