"""Веб-дашборд GLaDOS — оверлей для стримера. Порт 8766."""
import json
import logging
import os
import time
from pathlib import Path
from datetime import datetime

from aiohttp import web

import approvals
import game_bridge
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


def _read_say() -> dict | None:
    """Последняя реплика компаньона (любого возраста): по её метке времени питомец понимает, что реплика новая."""
    try:
        return json.loads((_MEMORY_DIR / "say.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_say() -> dict | None:
    """Свежая реплика компаньона (companion.py) — приоритетнее игрового комментария."""
    try:
        say = json.loads((_MEMORY_DIR / "say.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return say if time.time() - say.get("ts", 0) < 90 else None


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
        "approval":  {"pending": approvals.has_pending(), "text": approvals.pending_text()},
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


async def handle_api_approval(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
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
    game = await game_bridge.game_state()          # {} если агент RimWorld не запущен
    connected = bool(game.get("connected"))

    # Речь одна: питомец говорит и показывает ТОЛЬКО то, что прошло через компаньона (оно же в чате).
    say = _read_say()
    last_decision = ({"tick": None, "commentary": say["text"], "actions": [], "say_id": say.get("id")}
                     if say and time.time() - say.get("ts", 0) < 90 else None)   # совместимость
    data = {
        "connected":        connected,
        "colony_name":      game.get("colony_name"),
        "tick":             game.get("tick"),
        "pawns":            game.get("pawns", []),
        "resources":        game.get("resources", {}),
        "research":         game.get("research", {"current": None, "progress": 0.0}),
        "last_decision":    last_decision,
        "say":              say,
        "game_decision":    game.get("game_decision") if connected else None,
        "recent_decisions": game.get("recent_decisions", []),
        "ambition":         game.get("ambition"),
        "goals":            game.get("goals", []),
        "tasks":            game.get("tasks", []),
        "mode":             game.get("mode"),
        "pawn_fates":       game.get("pawn_fates", {}),
        "emotion":          _load_emotion(),
        "settings":         settings.load(),
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


# ── Каналы от агента RimWorld (отдельный процесс, только localhost) ───────────

_SPEECH = {"say": None, "status": None, "event": None}


def set_game_link(say=None, status=None, event=None):
    """say(text, emotion) -> корутина; status() -> dict; event(type, data) -> корутина. Ставит ядро."""
    _SPEECH.update(say=say, status=status, event=event)


async def handle_api_say(request: web.Request) -> web.Response:
    """Агент игры просит сказать реплику: она проходит через компаньона (чат, питомец, озвучка)."""
    if not _is_local_json(request) or _SPEECH["say"] is None:
        return web.json_response({"error": "unavailable"}, status=403)
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    await _SPEECH["say"](text, body.get("emotion"))
    return web.json_response({"ok": True})


async def handle_api_speech_status(request: web.Request) -> web.Response:
    """Что агенту игры нужно знать, чтобы не перебивать: когда пользователь говорил, говорит ли она, что сказала, настройки."""
    if _SPEECH["status"] is None:
        return web.json_response({})
    return web.json_response(_SPEECH["status"]())


async def handle_api_emotion(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    body = await request.json()
    import fast_brain
    if body.get("emotion") in fast_brain.EMOTIONS:
        fast_brain.publish(str(body.get("event") or "game"), {"emotion": body["emotion"], "action": "none",
                                                                "source": "game"})
    return web.json_response({"ok": True})


async def handle_api_event(request: web.Request) -> web.Response:
    """Прочие события игры: список колонистов для графа и отношений, итог забега для Telegram."""
    if not _is_local_json(request) or _SPEECH["event"] is None:
        return web.json_response({"error": "unavailable"}, status=403)
    body = await request.json()
    await _SPEECH["event"](str(body.get("type") or ""), body.get("data") or {})
    return web.json_response({"ok": True})


# ── Server lifecycle ─────────────────────────────────────────────────────────

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

    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_post("/api/chat", handle_api_chat_post)
    app.router.add_post("/api/settings", handle_api_settings_post)
    app.router.add_get("/api/core", handle_api_core)
    app.router.add_get("/api/events", handle_api_events)
    app.router.add_get("/api/capabilities", handle_api_capabilities)
    app.router.add_post("/api/approval", handle_api_approval)
    app.router.add_post("/api/memory/forget", handle_api_forget)
    app.router.add_get("/api/graph", handle_api_graph)
    app.router.add_post("/api/graph/forget", handle_api_graph_forget)
    app.router.add_get("/api/chat", handle_api_chat_get)
    app.router.add_post("/api/say", handle_api_say)
    app.router.add_get("/api/speech_status", handle_api_speech_status)
    app.router.add_post("/api/emotion", handle_api_emotion)
    app.router.add_post("/api/event", handle_api_event)

    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, os.getenv("GLADOS_DASHBOARD_HOST", "127.0.0.1"), port)
    await site.start()
    logger.info(f"Дашборд запущен: http://localhost:{port}")
