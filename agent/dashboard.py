"""Веб-дашборд ядра GitS — оверлей для стримера и пульт. Порт 8766."""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from datetime import datetime

from aiohttp import web

import approvals
import episode_routes
import lesson_routes
import events_log
import journal
import latency_trace
import module_panel
from gits_sdk.protocol import JOB_ID_RE
from memory_store import MemoryStore
import character
import settings
from user_memory import UserMemory
from envcompat import env

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


def _screen_state() -> dict | None:
    """Состояние модуля desktop для вкладки «Что она видит» (DESK-15): None, если модуль не подключён."""
    if _MODULES is None:
        return None
    rec = _MODULES.get("desktop")
    if rec is None or not rec.connected or not rec.state:
        return None
    data = rec.state.get("data") or {}
    return {"summary": rec.state.get("summary"), "foreground_app": data.get("foreground_app"),
            "sensitive": data.get("sensitive"), "idle_s": data.get("idle_s"), "activity": data.get("activity")}


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
        "screen":    _screen_state(),
        "memories":  sorted(MemoryStore(str(_char_dir())).all(), key=lambda i: -i["ts"])[:150],
        "relations": relations,
        "approval":  {"pending": approvals.has_pending(), "text": approvals.pending_text(),
                      **{k: v for k, v in (approvals.pending_info() or {}).items() if k in ("kind", "options", "source", "queued")}},
    })


async def handle_api_latency(request: web.Request) -> web.Response:
    """ТЗ 24, TRC-2: p50 и p95 по этапам реплики и от конца фразы до первого звука за окно (?window=секунды, час по умолчанию)."""
    try:
        window = max(60.0, min(float(request.query.get("window", "3600") or 3600), 7 * 86400.0))
    except ValueError:
        return web.json_response({"error": "bad params"}, status=400)
    return web.json_response(await asyncio.to_thread(latency_trace.latency_report, window))


async def handle_api_decisions(request: web.Request) -> web.Response:
    """ТЗ 24, TRC-6: выборка решений диспетчера для разметки (?limit=100&trace_id=...), только чтение."""
    try:
        limit = int(request.query.get("limit", "100") or 100)
    except ValueError:
        return web.json_response({"error": "bad params"}, status=400)
    rows = await asyncio.to_thread(journal.decisions, limit, request.query.get("trace_id") or None)
    return web.json_response({"decisions": rows})


async def handle_api_rating(request: web.Request) -> web.Response:
    """ТЗ 24, TRC-5: оценка ответа на пульте (value: bad | good) с trace_id реплики. Плохая оценка пишется как ошибка bad_rating."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):                       # список или строка в теле: не объект, отвечаем 400, а не 500
        return web.json_response({"error": "value: bad или good"}, status=400)
    value = body.get("value")
    trace_id = str(body.get("trace_id") or "")[:40] or None
    if value not in ("bad", "good"):
        return web.json_response({"error": "value: bad или good"}, status=400)
    if value == "bad":
        journal.mistake("bad_rating", "оценка на пульте: плохо", refs=[], trace_id=trace_id)
    else:
        events_log.log_nowait("reflex", "оценка на пульте: хорошо", trace_id=trace_id)
    return web.json_response({"ok": True})


async def handle_api_events(request: web.Request) -> web.Response:
    """Протокол событий: ?kind=say,user  &since=<ts>  &limit=200  &module=<id> (события модуля по полю data.module, TZ 15)."""
    q = request.query
    kinds = {k for k in q.get("kind", "").split(",") if k} or None
    try:
        since = float(q.get("since", "0") or 0)
        limit = max(1, min(int(q.get("limit", "200")), 500))
    except ValueError:
        return web.json_response({"error": "bad params"}, status=400)
    module = q.get("module", "")
    if module:
        rows = [e for e in events_log.tail(1000, kinds, since) if (e.get("data") or {}).get("module") == module]
        return web.json_response({"events": rows[-limit:]})
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


_ROUTER = None   # маршрутизатор (router.py), ставит ядро: нужен для POST /api/menu_click (ТЗ 14, DESK-14)


def set_router(router):
    global _ROUTER
    _ROUTER = router


async def handle_api_menu_click(request: web.Request) -> web.Response:
    """Нажать кнопку главного меню RimWorld без Claude и без вопроса: {"text": "Быстрый запуск"}. Прежний HTTP-путь
    для модуля RimWorld сохранён, но работа идёт инструментом desktop.menu_click (белый список кнопок и правило простоя
    живут в модуле desktop)."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if _ROUTER is None:
        return web.json_response({"ok": False, "result": "ядро ещё не готово"}, status=503)
    ok, result, code = await _ROUTER.run_tool_ex("desktop.menu_click", {"text": str(body.get("text") or "").strip()})
    if not ok and code == "denied":
        return web.json_response({"ok": False, "result": result}, status=403)
    return web.json_response({"ok": ok, "result": result})


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
        return web.json_response({"removed": int(MemoryStore(str(_char_dir())).delete(str(body["id"])))})
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    return web.json_response({"removed": UserMemory(str(_char_dir())).forget(text)})


_GRAPH = None   # GraphMemory, ставится ядром


_CHARACTER = None   # CharacterSwitch, ставится ядром (ТЗ 05, CHR-7)


def set_character(switch):
    global _CHARACTER
    _CHARACTER = switch


def _char_dir() -> Path:
    """Папка личной памяти активного персонажа (ТЗ 05, CHR-10): память дашборда показывает её."""
    return character.memory_dir(character.active().id, _MEMORY_DIR)


def _character_state() -> dict:
    ch = character.active()
    return {"id": ch.id, "name": ch.name, "ids": character.list_ids()}


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
        "character":        _character_state(),
        "presence":         _PRESENCE.snapshot() if _PRESENCE is not None else None,     # как тема presence (CORE-39)
        "modules":          ([{"id": m["id"], "connected": m["connected"], "enabled": m["enabled"]}
                              for m in _MODULES.snapshot()] if _MODULES is not None else []),
    }
    return web.json_response(data)


# ── Чат с персонажем ────────────────────────────────────────────────────────────

_CHAT_CALLBACK = None   # companion.on_user_message, проставляется в start_dashboard
_CHAT_LOG: list[dict] = []  # локальная история отправленных сообщений — для окна чата


async def handle_api_character_post(request: web.Request) -> web.Response:
    """Смена персонажа (ТЗ 05, CHR-7): тело {"id": "..."}; память и речь переключаются ядром."""
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    if _CHARACTER is None:
        return web.json_response({"error": "персонажи ещё не готовы"}, status=503)
    try:
        return web.json_response(await _CHARACTER.switch(str(body.get("id") or "")))
    except character.CharacterError as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_api_settings_post(request: web.Request) -> web.Response:
    if not _is_local_json(request):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    return web.json_response(settings.update(**{k: v for k, v in body.items()
                                                 if k in settings.DEFAULTS and k != "character"}))


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
    """Модули: состояние, инструменты, настройки (CORE-37). У каждой записи `kind`: module, service (компонент ядра) или
    planned (ещё не установлен); записи для разработчиков (echo) отдаются только при настройке dev_mode."""
    if _MODULES is None:
        return web.json_response([])
    dev_mode = bool(await asyncio.to_thread(lambda: settings.load().get("dev_mode")))
    return web.json_response(_MODULES.dashboard_rows(dev_mode))


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


# ── Пространство модуля (TZ 15) ──────────────────────────────────────────────

PANEL_ACTION_TIMEOUT_S = 15.0       # panel.action: ответ модуля не позже


async def handle_api_module_panel(request: web.Request) -> web.Response:
    """Пространство модуля: объявленное в манифесте `panel`, последние блоки `panel.set` и состояние подключения."""
    rec, err = _module_or_404(request)
    if err:
        return err
    stored = rec.panel_blocks or {}
    return web.json_response({"id": rec.id, "panel": rec.panel, "blocks": stored.get("blocks", []),
                              "ts": stored.get("ts"), "connected": rec.connected})


async def handle_api_module_panel_action(request: web.Request) -> web.Response:
    """{"action_id": str, "args": {}}: кнопка или строка пространства. Идентификатор должен быть в panel.actions;
    модуль отвечает `panel.action`, адрес открытия проверяется (`module_panel.action_result`)."""
    body, err = await _json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    action_id, args = body.get("action_id"), body.get("args", {})
    if not isinstance(action_id, str) or action_id not in ((rec.panel or {}).get("actions") or []):
        return web.json_response({"error": "действие не объявлено в panel.actions модуля"}, status=400)
    if not isinstance(args, dict):
        return web.json_response({"error": "args должно быть объектом"}, status=400)
    if _HUB is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = _HUB.connection(rec.id)
    if conn is None:
        return web.json_response({"error": "модуль не подключён"}, status=409)
    from module_hub import ModuleError
    try:
        value = await conn.request("panel.action", {"action_id": action_id, "args": args},
                                   timeout=PANEL_ACTION_TIMEOUT_S)
    except ModuleError as e:                       # отказ модуля - обычный ответ, не ошибка дашборда
        return web.json_response({"ok": False, "message": e.message, "code": e.code})
    return web.json_response(module_panel.action_result(value, (rec.panel or {}).get("allowed_hosts")))


async def handle_api_module_panel_link(request: web.Request) -> web.Response:
    """{"link_id": str, "job_id": str?}: нажатие ссылки пространства из `panel.links`.
    url - адрес из манифеста; reveal_path - папка проводника, только внутри serve_dirs; tool - инструмент модуля с
    риском auto (остальные идут через разговор), с аргументом job_id, если в ссылке args_from: job."""
    body, err = await _json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    panel = rec.panel or {}
    link = next((x for x in panel.get("links") or [] if x.get("id") == body.get("link_id")), None)
    if link is None:
        return web.json_response({"error": "нет такой ссылки у модуля"}, status=404)
    kind = link.get("kind")
    if kind == "url":
        return web.json_response({"ok": True, "open_url": link["url"]})
    if kind == "reveal_path":
        raw = link.get("path") or ((rec.state or {}).get("data") or {}).get(link.get("path_from"))
        folder = module_panel.served_dir(panel.get("serve_dirs"), raw)
        if folder is None:
            return web.json_response({"error": "папка вне разрешённых каталогов модуля"}, status=403)
        try:
            await asyncio.to_thread(module_panel.reveal, folder)
        except RuntimeError as e:
            return web.json_response({"ok": False, "message": str(e)}, status=501)
        return web.json_response({"ok": True})
    return await _run_tool_link(rec, link, body)


async def _run_tool_link(rec, link: dict, body: dict) -> web.Response:
    spec = next((t for t in (rec.manifest or {}).get("tools") or [] if t.get("name") == link.get("tool")), None)
    if spec is None:
        return web.json_response({"error": "инструмент ссылки не найден в манифесте"}, status=400)
    if spec.get("risk") != "auto":
        return web.json_response({"error": "инструмент требует разрешения: запускается через разговор"}, status=403)
    args: dict = {}
    if link.get("args_from") == "job":
        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id) or job_id not in rec.jobs:
            return web.json_response({"error": "нужна задача этого модуля (job_id)"}, status=400)
        args["job_id"] = job_id
    if _HUB is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = _HUB.connection(rec.id)
    if conn is None:
        return web.json_response({"error": "модуль не подключён"}, status=409)
    from module_hub import ModuleError
    try:
        value = await conn.request("tool.call", {"tool": spec["name"], "args": args},
                                   timeout=float(spec.get("timeout_s", 30)))
    except ModuleError as e:
        return web.json_response({"ok": False, "message": e.message, "code": e.code})
    out: dict = {"ok": True}
    if link.get("open_result_url") and isinstance(value, dict) and isinstance(value.get("url"), str):
        if module_panel.open_url_allowed(value["url"], (rec.panel or {}).get("allowed_hosts")):
            out["open_url"] = value["url"]
        else:
            out["message"] = "адрес не открыт: хост не разрешён"
    return web.json_response(out)


async def handle_api_artifact(request: web.Request) -> web.Response:
    """Файл артефакта задачи: `/api/artifact?module=&job=&name=` (TZ 15, TAB-3). Путь берётся из артефактов задачи и
    раздаётся только из каталогов panel.serve_dirs; диапазоны (Range) для видео отдаёт aiohttp."""
    if _MODULES is None:
        return web.json_response({"error": "модули не подключены"}, status=503)
    q = request.query
    rec = _MODULES.get(q.get("module", ""))
    if rec is None:
        return web.json_response({"error": "нет такого модуля"}, status=404)
    job_id, name = q.get("job", ""), q.get("name", "")
    if not JOB_ID_RE.match(job_id) or module_panel.bad_name(name):
        return web.json_response({"error": "нужны job (j-xxxxxxxx) и name (имя файла)"}, status=400)
    raw = module_panel.find_artifact(rec.jobs, job_id, name)
    if raw is None:
        return web.json_response({"error": "такого артефакта у задачи нет"}, status=404)
    path = module_panel.served_file((rec.panel or {}).get("serve_dirs"), raw)
    if path is None:
        return web.json_response({"error": "файл вне разрешённых каталогов модуля"}, status=403)
    resp = web.FileResponse(path, headers={"Cache-Control": "no-cache"})
    resp.content_type = module_panel.artifact_content_type(path)
    return resp


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


_FOCUS = None      # менеджер фокуса (ТЗ 18), ставит ядро


_IMPULSES = None   # импульсы и размышление (ТЗ 30), ставит ядро


def set_impulses(impulses):
    global _IMPULSES
    _IMPULSES = impulses


async def handle_api_thoughts(request: web.Request) -> web.Response:
    """Журнал мыслей (ТЗ 30, IMP-6) и ожидающие напоминания себе. Мысли не озвучиваются."""
    if _IMPULSES is None:
        return web.json_response({"thoughts": [], "reminders": []})
    try:
        n = max(1, min(200, int(request.query.get("n", 50))))
    except ValueError:
        n = 50
    thoughts = await asyncio.to_thread(_IMPULSES.journal.recent, n)
    reminders = await asyncio.to_thread(_IMPULSES.reminders.pending)
    return web.json_response({"thoughts": thoughts, "reminders": reminders})


def set_focus(focus):
    global _FOCUS
    _FOCUS = focus


_AFFECT = None      # AffectService (ТЗ 26), ставит ядро


def set_affect(affect) -> None:
    global _AFFECT
    _AFFECT = affect


async def handle_api_affect(request: web.Request) -> web.Response:
    """Состояние эмоций (ТЗ 26, AFF-7): настроение, эмоции, график за 24 часа."""
    if _AFFECT is None:
        return web.json_response({"available": False})
    return web.json_response({"available": True, **_AFFECT.snapshot(with_history=True)})


async def handle_api_affect_reset(request: web.Request) -> web.Response:
    if _AFFECT is None:
        return web.json_response({"available": False})
    _AFFECT.reset()
    return web.json_response({"available": True, **_AFFECT.snapshot(with_history=True)})


async def handle_api_focus(request: web.Request) -> web.Response:
    """Фокус (ТЗ 18, FOC-9): кто в фокусе, очередь, checkpoint, журнал переходов, политики модулей."""
    if _FOCUS is None:
        return web.json_response({"holder": None, "queue": [], "checkpoints": [], "journal": [], "policies": {}})
    return web.json_response(_FOCUS.snapshot())


async def handle_api_focus_pin(request: web.Request) -> web.Response:
    """{"module": "rimworld"} закрепить фокус за модулем; {"module": null} снять закрепление."""
    body, err = await _json_body(request)
    if err:
        return err
    if _FOCUS is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module = body.get("module")
    if module is not None and (not isinstance(module, str) or not module):
        return web.json_response({"error": "module: строка или null"}, status=400)
    return web.json_response(await _FOCUS.pin(module))


async def handle_api_focus_dnd(request: web.Request) -> web.Response:
    """{"module": "telegram" | "all", "on": true|false} - «не беспокоить» для модуля или для всего агента."""
    body, err = await _json_body(request)
    if err:
        return err
    if _FOCUS is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module, on = body.get("module", "all"), body.get("on")
    if not isinstance(module, str) or not module or not isinstance(on, bool):
        return web.json_response({"error": "нужны module (строка) и on (true или false)"}, status=400)
    return web.json_response(await _FOCUS.set_dnd(module, on))


async def handle_api_focus_once(request: web.Request) -> web.Response:
    """{"module": "telegram"} - разовое исключение из «не беспокоить» для следующего обычного события модуля."""
    body, err = await _json_body(request)
    if err:
        return err
    if _FOCUS is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module = body.get("module")
    if not isinstance(module, str) or not module:
        return web.json_response({"error": "нужен module (строка)"}, status=400)
    return web.json_response(await _FOCUS.allow_once(module))


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


NOTIFY_TEXT_MAX = 300
_NOTIFY_SOURCE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_LOCAL_PEERS = ("127.0.0.1", "::1")


async def handle_api_notify(request: web.Request) -> web.Response:
    """Объявление от внешнего скрипта или теста (не модуль): `{"text": str до 300 символов, "source": "rimworld-tests"}`.
    Идёт как объявление о запуске (AN-2, `announce: true`) от `source`: мимо формулы важности, звучит всегда (коротко при
    «не отвлекать», в сводку, если пользователь отошёл); текст она произнесёт своими словами.
    Только с этого же компьютера и только JSON."""
    if request.remote not in _LOCAL_PEERS:
        return web.json_response({"error": "forbidden"}, status=403)
    body, err = await _json_body(request)
    if err:
        return err
    text, source = body.get("text"), body.get("source")
    if not isinstance(text, str) or not text.strip() or len(text) > NOTIFY_TEXT_MAX:
        return web.json_response({"error": f"text: непустая строка не длиннее {NOTIFY_TEXT_MAX} символов"}, status=400)
    if not isinstance(source, str) or not _NOTIFY_SOURCE.match(source):
        return web.json_response({"error": "source: короткое имя из латиницы, цифр и _.-"}, status=400)
    if _ATTENTION is None:
        return web.json_response({"error": "внимание не подключено"}, status=503)
    await _ATTENTION.submit_speech(source, {"text": text.strip(), "mode": "fact", "urgency_hint": 3,
                                                  "announce": True})
    return web.json_response({"ok": True})


# ── Server lifecycle ─────────────────────────────────────────────────────────

def build_app() -> web.Application:
    """Приложение дашборда: страницы, API и шина модулей (/module/ws)."""
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/api/state", handle_api_state)
    app.router.add_post("/api/chat", handle_api_chat_post)
    app.router.add_post("/api/settings", handle_api_settings_post)
    app.router.add_post("/api/character", handle_api_character_post)
    app.router.add_get("/api/core", handle_api_core)
    app.router.add_get("/api/events", handle_api_events)
    app.router.add_get("/api/latency", handle_api_latency)
    app.router.add_post("/api/rating", handle_api_rating)
    app.router.add_get("/api/decisions", handle_api_decisions)
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
    app.router.add_get("/api/modules/{id}/panel", handle_api_module_panel)
    app.router.add_post("/api/modules/{id}/panel/action", handle_api_module_panel_action)
    app.router.add_post("/api/modules/{id}/panel/link", handle_api_module_panel_link)
    app.router.add_get("/api/artifact", handle_api_artifact)
    app.router.add_get("/api/attention", handle_api_attention)
    app.router.add_post("/api/attention/rules/clear", handle_api_attention_clear)
    app.router.add_get("/api/focus", handle_api_focus)
    app.router.add_get("/api/affect", handle_api_affect)
    app.router.add_post("/api/affect/reset", handle_api_affect_reset)
    app.router.add_get("/api/thoughts", handle_api_thoughts)
    episode_routes.add_routes(app)                      # ТЗ 27: эпизоды, дневник, сон
    lesson_routes.add_routes(app)                       # ТЗ 28: уроки, убеждения, отмена
    app.router.add_post("/api/focus/pin", handle_api_focus_pin)
    app.router.add_post("/api/focus/dnd", handle_api_focus_dnd)
    app.router.add_post("/api/focus/allow_once", handle_api_focus_once)
    app.router.add_post("/api/notify", handle_api_notify)
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
    site = web.TCPSite(runner, env("GITS_DASHBOARD_HOST", "127.0.0.1"), port)
    await site.start()
    logger.info(f"Дашборд запущен: http://localhost:{port}")
