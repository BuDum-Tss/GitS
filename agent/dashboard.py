"""Веб-дашборд GLaDOS — оверлей для стримера. Порт 8766."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime

from aiohttp import web

import approvals
from memory_store import MemoryStore
import events_log
import settings
from user_memory import UserMemory

logger = logging.getLogger("dashboard")

_MEMORY_DIR: Path = Path("./memory")

# ── HTML ────────────────────────────────────────────────────────────────────

_UI_FILE = Path(__file__).parent / "static" / "ui" / "index.html"


# ── API handlers ────────────────────────────────────────────────────────────

def _load_emotion() -> dict | None:
    """Свежая эмоция от быстрого слоя (fast_brain) или None."""
    f = _MEMORY_DIR / "emotion.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_local_json(request: web.Request) -> bool:
    """Защита от чужих запросов: только JSON (браузерный cross-site запрос так не
    отправить без CORS-разрешения) и только со своей же страницы."""
    if request.content_type != "application/json":
        return False
    origin = request.headers.get("Origin")
    if origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")):
        return False
    return True


async def handle_root(request: web.Request) -> web.Response:
    try:
        html = _UI_FILE.read_text(encoding="utf-8")
    except OSError:
        return web.Response(text="UI не найден: static/ui/index.html", status=500)
    return web.Response(text=html, content_type="text/html")


def _read_json(name: str, default):
    try:
        return json.loads((_MEMORY_DIR / name).read_text(encoding="utf-8"))
    except Exception:
        return default


async def handle_api_core(request: web.Request) -> web.Response:
    """Всё, что показывает UI помимо игры: разговор, зрение, память, вопрос о разрешении."""
    from relationships import is_hidden, kind_of, label
    rel = _read_json("relationships.json", {})
    rows = sorted(
        ({"name": r.get("name", n), "affinity": r.get("affinity", 0), "label": label(r.get("affinity", 0)),
          "alive": r.get("alive", True), "kind": kind_of(n, r), "source": r.get("source", ""),
          "last": (r.get("notes") or [""])[-1]} for n, r in rel.items()
         if not is_hidden(n, r, time.time())),
        key=lambda x: -abs(x["affinity"]))
    relations = [x for x in rows if x["kind"] != "pawn"][:12] + [x for x in rows if x["kind"] == "pawn"][:12]
    return web.json_response({
        "dialogue":  _read_json("dialogue.json", [])[-40:],
        "activity":  _read_json("activity.json", [])[-5:],
        "memories":  sorted(MemoryStore(str(_MEMORY_DIR)).all(), key=lambda i: -i["ts"])[:150],
        "relations": relations,
        "approval":  {"pending": approvals.has_pending(), "text": approvals.pending_text(),
                      **{k: v for k, v in (approvals.pending_info() or {}).items() if k in ("kind", "options", "source", "queued")}},
    })


async def handle_api_events(request: web.Request) -> web.Response:
    """Протокол событий: ?kind=say,user  &since=<ts>  &limit=200"""
    q = request.query
    kinds = {k for k in q.get("kind", "").split(",") if k} or None
    try:
        since = float(q.get("since", "0") or 0)
        limit = max(1, min(int(q.get("limit", "200")), 500))
    except ValueError:
        return web.json_response({"error": "bad params"}, status=400)
    return web.json_response({"events": events_log.tail(limit, kinds, since)})


_REGISTRY = None   # реестр окружений, ставится ядром


def set_registry(registry):
    global _REGISTRY
    _REGISTRY = registry


async def handle_api_capabilities(request: web.Request) -> web.Response:
    """Окружения и их инструменты: что ассистентка реально умеет."""
    if _REGISTRY is None:
        return web.json_response({"environments": [], "errors": {}})
    return web.json_response({"environments": _REGISTRY.snapshot(), "errors": _REGISTRY.load_errors})


_MODES = None   # ProfileManager (profile_manager.py), ставится ядром


def set_modes(manager):
    global _MODES
    _MODES = manager


async def handle_api_modes(request: web.Request) -> web.Response:
    """Граф профилей: состояния, переходы, подграфы (живые - от модулей профиля)."""
    if _MODES is None:
        return web.json_response({"nodes": [], "edges": [], "current": None})
    g = await _MODES.graph()
    g["flags"] = {"streamer": bool(settings.load().get("streamer"))}   # стример: не профиль, а переключатель состояния
    return web.json_response(g)


async def handle_api_mode_post(request: web.Request) -> web.Response:
    """Ручной выбор профиля: {"mode": "rimworld"} или {"mode": "auto"} (пишет настройку profile)."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if _MODES is None:
        return web.json_response({"error": "режимы не подключены"}, status=503)
    err = _MODES.set_override(body.get("mode"))
    if err:
        return web.json_response({"error": err}, status=400)
    return web.json_response({"current": _MODES.current_id, "override": _MODES.override})


_MENU_BUTTONS = ("быстрый запуск", "quick start", "продолжить", "continue", "загрузить", "load")   # только кнопки главного меню


async def handle_api_menu_click(request: web.Request) -> web.Response:
    """Нажать кнопку главного меню RimWorld без Claude и без вопроса: {"text": "Быстрый запуск"} (OCR по окну игры).
    Узко и безопасно: только надписи из белого списка, только когда окно с RimWorld найдено."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    text = str(body.get("text") or "").strip()
    if text.lower() not in _MENU_BUTTONS:
        return web.json_response({"ok": False, "result": "такой кнопки в списке разрешённых нет"}, status=403)
    import perception
    if perception.idle_seconds() < 120:       # мышь настоящая: не перехватываем её, пока пользователь за компьютером
        return web.json_response({"ok": False, "result": "пользователь сейчас за компьютером: жду, пока отойдёт"})
    from environments import pc_control
    import asyncio

    def work():
        pc_control._focus_window("RimWorld")
        import time as _t
        _t.sleep(0.6)
        return pc_control._click_text(text, "left", False)
    try:
        return web.json_response({"ok": True, "result": await asyncio.to_thread(work)})
    except Exception as e:
        return web.json_response({"ok": False, "result": str(e)})


def _rimworld_url() -> str:
    """Адрес API модуля RimWorld (пульт отметок оценок): RIMWORLD_MODULE_URL, запасная старая RIMWORLD_AGENT_URL (ТЗ 11, RW-14b)."""
    if os.getenv("RIMWORLD_MODULE_URL"):
        return os.environ["RIMWORLD_MODULE_URL"]
    if os.getenv("RIMWORLD_AGENT_URL"):
        logger.warning("Переменная RIMWORLD_AGENT_URL устарела: переименуй в RIMWORLD_MODULE_URL")
        return os.environ["RIMWORLD_AGENT_URL"]
    return "http://127.0.0.1:8767"


async def handle_api_appraisal_label(request: web.Request) -> web.Response:
    """Отметка игрока по оценке события: пересылается модулю игры, у которого журнал (по HTTP: это пульт, не шина модулей)."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    import httpx
    base = _rimworld_url()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{base}/api/appraisal_label", json=body)
            return web.json_response(r.json(), status=r.status_code)
    except Exception as e:
        return web.json_response({"saved": False, "error": str(e)}, status=502)


async def handle_api_approval(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if "answer" in body:                      # choice: вариант, text: строка, approval: true/false
        return web.json_response({"resolved": approvals.answer(body["answer"])})
    return web.json_response({"resolved": approvals.resolve(bool(body.get("allow")))})


async def handle_api_forget(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if body.get("id"):
        return web.json_response({"removed": int(MemoryStore(str(_MEMORY_DIR)).delete(str(body["id"])))})
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    return web.json_response({"removed": UserMemory(str(_MEMORY_DIR)).forget(text)})


_GRAPH = None   # GraphMemory, ставится ядром


def set_graph(graph):
    global _GRAPH
    _GRAPH = graph


async def handle_api_graph(request: web.Request) -> web.Response:
    """Граф памяти: действующие факты, устаревшие и состояние (очередь, база)."""
    if _GRAPH is None:
        return web.json_response({"enabled": False, "status": "память-граф не подключена"})
    snap = await _GRAPH.snapshot()
    edges = snap.get("edges", [])
    return web.json_response({
        "enabled": True, "status": _GRAPH.status_text(), "pending": _GRAPH.pending(), "nodes": snap.get("entities", []),
        "current": [e for e in edges if not e["invalid_at"]], "outdated": [e for e in edges if e["invalid_at"]],
        "entities": len(snap.get("entities", [])), "error": snap.get("error"),
    })


async def handle_api_graph_forget(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if _GRAPH is None or not body.get("uuid"):
        return web.json_response({"error": "нечего удалять"}, status=400)
    return web.json_response({"removed": await _GRAPH.forget(str(body["uuid"]))})


async def handle_api_state(request: web.Request) -> web.Response:
    game = (_MODULES.state_data("rimworld") if _MODULES is not None else None) or {}    # state модуля игры; {} если он не подключён
    connected = bool(game.get("connected"))

    # Реплика ассистентки (PET-10): файла реплики и полей say / last_decision больше нет; питомец и другие модули получают речь толчком,
    # темой presence (speaking, segment, last_say), а не опросом этого эндпоинта.
    data = {
        "connected":        connected,
        "colony_name":      game.get("colony_name"),
        "tick":             game.get("tick"),
        "pawns":            game.get("pawns", []),
        "resources":        game.get("resources", {}),
        "research":         game.get("research", {"current": None, "progress": 0.0}),
        "game_decision":    game.get("game_decision") if connected else None,
        "recent_decisions": game.get("recent_decisions", []),
        "ambition":         game.get("ambition"),
        "goals":            game.get("goals", []),
        "tasks":            game.get("tasks", []),
        "mode":             game.get("mode"),
        "pawn_fates":       game.get("pawn_fates", {}),
        "emotion":          _load_emotion(),
        "settings":         settings.load(),
        "presence":         _PRESENCE.snapshot() if _PRESENCE is not None else None,     # как тема presence (CORE-39)
        "modules":          ([{"id": m["id"], "connected": m["connected"], "enabled": m["enabled"]}
                              for m in _MODULES.snapshot()] if _MODULES is not None else []),
    }
    return web.json_response(data)


# ── Чат с GLaDOS ────────────────────────────────────────────────────────────

_CHAT_CALLBACK = None   # companion.on_user_message, проставляется в start_dashboard
_CHAT_LOG: list[dict] = []  # локальная история отправленных сообщений — для окна чата


async def handle_api_settings_post(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    return web.json_response(settings.update(**{k: v for k, v in body.items()
                                                 if k in settings.DEFAULTS}))


async def handle_api_chat_post(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    message = (body.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    _CHAT_LOG.append({"text": message, "ts": datetime.now().isoformat()})
    del _CHAT_LOG[:-50]

    if _CHAT_CALLBACK:
        _CHAT_CALLBACK(message)

    return web.json_response({"status": "ok"})


async def handle_api_chat_get(request: web.Request) -> web.Response:
    return web.json_response({"messages": _CHAT_LOG[-20:]})


# ── Модули и задачи (ТЗ 02, CORE-37) ───────────────────────────────────────────

_MODULES = None       # ModuleRegistry
_HUB = None           # ModuleHub
_SUPERVISOR = None    # Supervisor
_PRESENCE = None      # PresenceTracker
_BG_TASKS: set = set()


def set_modules(modules, hub, supervisor=None):
    """Реестр модулей, шина и супервизор: ставит ядро."""
    global _MODULES, _HUB, _SUPERVISOR
    _MODULES, _HUB, _SUPERVISOR = modules, hub, supervisor


def set_presence(presence):
    global _PRESENCE
    _PRESENCE = presence


def _background(coro):
    """Долгое дело (остановка процесса до 10 с) уходит в фон: кнопка в дашборде отвечает сразу."""
    task = asyncio.get_running_loop().create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _json_body(request: web.Request):
    """Тело POST как объект или ответ-ошибка: (body, None) или (None, response)."""
    if not _is_local_json(request):
        return None, web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response({"error": "тело должно быть объектом"}, status=400)
    return body, None


def _module_or_404(request: web.Request):
    if _MODULES is None:
        return None, web.json_response({"error": "модули не подключены"}, status=503)
    rec = _MODULES.get(request.match_info["id"])
    if rec is None:
        return None, web.json_response({"error": "нет такого модуля"}, status=404)
    return rec, None


async def handle_api_modules(request: web.Request) -> web.Response:
    """Модули: состояние, инструменты, настройки (CORE-37)."""
    return web.json_response(_MODULES.snapshot() if _MODULES is not None else [])


async def handle_api_module_enabled(request: web.Request) -> web.Response:
    """{"enabled": bool}: включить или выключить модуль; процесс запускает или останавливает супервизор."""
    body, err = await _json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    if not isinstance(body.get("enabled"), bool):
        return web.json_response({"error": "enabled должно быть true или false"}, status=400)
    await asyncio.to_thread(lambda: settings.update(modules={rec.id: {"enabled": body["enabled"]}}))
    _MODULES.invalidate()
    if _HUB is not None:
        await _HUB.refresh()                                  # модулю уходит тема module (MOD-14)
    if _SUPERVISOR is not None and rec.id in _SUPERVISOR.specs:
        _background(_SUPERVISOR.apply(rec.id))
    return web.json_response({"ok": True, "enabled": body["enabled"]})


async def handle_api_module_restart(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    if _SUPERVISOR is None or rec.id not in _SUPERVISOR.specs:
        return web.json_response({"error": "этот модуль запущен не ядром, перезапустить его можно только вручную"}, status=400)
    _background(_SUPERVISOR.restart(rec.id))
    return web.json_response({"ok": True})


def _check_setting_value(spec: dict, value):
    """Значение настройки по схеме модуля (ТЗ 01, 6.3): (значение, ошибка)."""
    kind = spec.get("type")
    if kind == "boolean":
        ok = isinstance(value, bool)
    elif kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, str)
    if not ok:
        return None, f"значение должно быть типа {kind}"
    if kind in ("integer", "number"):
        if "min" in spec and value < spec["min"]:
            return None, f"значение меньше {spec['min']}"
        if "max" in spec and value > spec["max"]:
            return None, f"значение больше {spec['max']}"
    if spec.get("enum") and value not in spec["enum"]:
        return None, f"значение должно быть одним из {spec['enum']}"
    return value, None


async def handle_api_module_settings(request: web.Request) -> web.Response:
    """{"key": str, "value": any}: собственная настройка модуля по его settings_schema."""
    body, err = await _json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    schema = (rec.manifest or {}).get("settings_schema") or {}
    key = body.get("key")
    if key not in schema:
        return web.json_response({"error": f"у модуля нет настройки «{key}»"}, status=400)
    value, problem = _check_setting_value(schema[key], body.get("value"))
    if problem:
        return web.json_response({"error": problem}, status=400)
    await asyncio.to_thread(lambda: settings.update(modules={rec.id: {key: value}}))
    _MODULES.invalidate()
    if _HUB is not None:
        await _HUB.refresh()
    return web.json_response({"ok": True, "module_settings": _MODULES.module_settings(rec.id)})


async def handle_api_jobs(request: web.Request) -> web.Response:
    """Задачи всех модулей."""
    keys = ("module", "job_id", "title", "state", "stage", "progress", "text", "question", "artifacts", "updated")
    rows = [{k: j.get(k) for k in keys} for j in (_MODULES.jobs_all() if _MODULES is not None else [])]
    return web.json_response(rows)


async def handle_api_job_cancel(request: web.Request) -> web.Response:
    body, err = await _json_body(request)
    if err:
        return err
    if _HUB is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = _HUB.connection(request.match_info["module"])
    if conn is None:
        return web.json_response({"error": "модуль не подключён"}, status=409)
    from module_hub import ModuleError
    try:
        await conn.request("job.cancel", {"job_id": request.match_info["job_id"]}, timeout=15.0)
    except ModuleError as e:
        return web.json_response({"error": e.message, "code": e.code}, status=400)
    return web.json_response({"ok": True})


async def handle_api_job_answer(request: web.Request) -> web.Response:
    """{"question_id": str, "answer": any}: ответ на вопрос задачи кнопкой; доходит до модуля как job.answer."""
    body, err = await _json_body(request)
    if err:
        return err
    if _HUB is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    problem = await _HUB.answer_job(request.match_info["module"], request.match_info["job_id"],
                                    str(body.get("question_id") or ""), body.get("answer"))
    if problem:
        return web.json_response({"error": problem}, status=400)
    return web.json_response({"ok": True})


# ── Внимание (ТЗ 03) ───────────────────────────────────────────────────────────

_ATTENTION = None     # Attention (attention.py), ставит ядро


def set_attention(attention):
    global _ATTENTION
    _ATTENTION = attention


async def handle_api_attention(request: web.Request) -> web.Response:
    """Правила, очередь речи, стек фокуса, сводка, последние решения и настройки внимания."""
    if _ATTENTION is None:
        return web.json_response({"rules": [], "queue": [], "focus": [], "digest": {}, "decisions": [], "settings": {}})
    return web.json_response(_ATTENTION.snapshot())


async def handle_api_attention_clear(request: web.Request) -> web.Response:
    """{"id": "r-xxxxxx"} снять правило; без id снять все."""
    body, err = await _json_body(request)
    if err:
        return err
    if _ATTENTION is None:
        return web.json_response({"error": "внимание не подключено"}, status=503)
    rule_id = body.get("id")
    if rule_id is not None and not isinstance(rule_id, str):
        return web.json_response({"error": "id должен быть строкой"}, status=400)
    return web.json_response({"ok": True, "text": await _ATTENTION.clear_rules(rule_id)})


# ── Server lifecycle ─────────────────────────────────────────────────────────

def build_app() -> web.Application:
    """Приложение дашборда: страницы, API и шина модулей (/module/ws)."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_post("/api/chat", handle_api_chat_post)
    app.router.add_post("/api/settings", handle_api_settings_post)
    app.router.add_get("/api/core", handle_api_core)
    app.router.add_get("/api/events", handle_api_events)
    app.router.add_get("/api/capabilities", handle_api_capabilities)
    app.router.add_get("/api/modes", handle_api_modes)
    app.router.add_post("/api/appraisal_label", handle_api_appraisal_label)
    app.router.add_post("/api/menu_click", handle_api_menu_click)
    app.router.add_post("/api/mode", handle_api_mode_post)
    app.router.add_post("/api/approval", handle_api_approval)
    app.router.add_post("/api/memory/forget", handle_api_forget)
    app.router.add_get("/api/graph", handle_api_graph)
    app.router.add_post("/api/graph/forget", handle_api_graph_forget)
    app.router.add_get("/api/chat", handle_api_chat_get)
    app.router.add_get("/api/modules", handle_api_modules)
    app.router.add_post("/api/modules/{id}/enabled", handle_api_module_enabled)
    app.router.add_post("/api/modules/{id}/restart", handle_api_module_restart)
    app.router.add_post("/api/modules/{id}/settings", handle_api_module_settings)
    app.router.add_get("/api/attention", handle_api_attention)
    app.router.add_post("/api/attention/rules/clear", handle_api_attention_clear)
    app.router.add_get("/api/jobs", handle_api_jobs)
    app.router.add_post("/api/jobs/{module}/{job_id}/cancel", handle_api_job_cancel)
    app.router.add_post("/api/jobs/{module}/{job_id}/answer", handle_api_job_answer)
    if _HUB is not None:
        _HUB.attach(app)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir)
    return app


async def start_dashboard(
    memory_dir: str = "./memory",
    port: int = 8766,
    chat_callback=None,
):
    global _MEMORY_DIR, _CHAT_CALLBACK
    _MEMORY_DIR = Path(memory_dir)
    settings.init(memory_dir)
    events_log.init(memory_dir)
    _CHAT_CALLBACK = chat_callback

    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, os.getenv("GLADOS_DASHBOARD_HOST", "127.0.0.1"), port)
    await site.start()
    logger.info(f"Дашборд запущен: http://localhost:{port}")
