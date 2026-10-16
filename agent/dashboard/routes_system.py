"""Роутер дашборда: страница, состояние, журналы, оценки, настройки, профили, персонаж (ТЗ 29, ARC-8)."""
import asyncio
import json
import os
import time
from pathlib import Path

import character
import events_log
import flow_spec
import flow_trace
import journal
import latency_trace
from aiohttp import web

from . import presenters
from .context import ctx, raw_json_body

_UI_FILE = Path(__file__).resolve().parents[1] / "static" / "ui" / "index.html"


def read_json(name: str, default):
    try:
        return json.loads((ctx.memory_dir / name).read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_emotion() -> dict | None:
    """Свежая эмоция от быстрого слоя (fast_brain) или None."""
    try:
        return json.loads((ctx.memory_dir / "emotion.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def char_dir() -> Path:
    """Папка личной памяти активного персонажа (ТЗ 05, CHR-10): память дашборда показывает её."""
    return character.memory_dir(character.active().id, ctx.memory_dir)


def _character_state() -> dict:
    ch = character.active()
    return {"id": ch.id, "name": ch.name, "ids": character.list_ids()}


async def handle_root(request: web.Request) -> web.Response:
    try:
        html = _UI_FILE.read_text(encoding="utf-8")
    except OSError:
        return web.Response(text="UI не найден: static/ui/index.html", status=500)
    return web.Response(text=html, content_type="text/html")


async def handle_api_state(request: web.Request) -> web.Response:
    game = (ctx.modules.state_data("rimworld") if ctx.modules is not None else None) or {}    # state модуля игры; {} если он не подключён
    return web.json_response(presenters.state_payload(
        game=game, emotion=_load_emotion(), settings_dict=ctx.settings.load(), character=_character_state(),
        presence=ctx.presence.snapshot() if ctx.presence is not None else None,
        modules=ctx.modules.snapshot() if ctx.modules is not None else []))


def _module_ids() -> list[str]:
    try:
        return [m["id"] for m in (ctx.modules.snapshot() if ctx.modules is not None else []) if m.get("id")]
    except Exception:
        return []


async def handle_api_flow(request: web.Request) -> web.Response:
    """ТЗ 17, FLOW-6: топология потока данных, счётчики рёбер и последние ходы (трасса и журнал задержки ТЗ 24)."""
    def build():
        rows = events_log.tail(limit=40, kinds={"trace"}, since=time.time() - 3600)
        lat = [t for t in (flow_trace.turn_from_latency_row(r) for r in rows) if t]
        return lat[-20:][::-1]
    latency = await asyncio.to_thread(build)
    return web.json_response({**flow_spec.topology(_module_ids()), "stats": flow_trace.edge_stats(),
                              "turns": flow_trace.turns(30), "latency_turns": latency,
                              "errors": flow_trace.error_nodes(), "only_core": os.environ.get("GITS_NO_MODULES") == "1" or bool(ctx.settings.load().get("only_core"))})


async def handle_api_flow_node(request: web.Request) -> web.Response:
    return web.json_response(flow_trace.node_info(request.match_info["node"]))


async def handle_api_flow_turn(request: web.Request) -> web.Response:
    """Шаги одного хода (FLOW-9: экспорт трассы хода в JSON)."""
    tid = request.match_info["turn"]
    steps = flow_trace.turn_steps(tid)
    if not steps:
        rows = await asyncio.to_thread(events_log.tail, limit=2000, kinds={"trace"}, since=time.time() - 86400)
        for r in rows:
            t = flow_trace.turn_from_latency_row(r)
            if t and t["turn"] == tid:
                return web.json_response({"turn": tid, "source": "latency", **t})
        return web.json_response({"error": "ход не найден"}, status=404)
    return web.json_response({"turn": tid, "source": "flow", "steps": steps, "outcome": flow_trace.outcome(steps)})


async def handle_api_flow_stream(request: web.Request) -> web.StreamResponse:
    """SSE: живая трасса (записи без текста)."""
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    last = (flow_trace.records(limit=1) or [{"seq": 0}])[-1]["seq"]
    try:
        while True:
            fresh = flow_trace.records(since_seq=last)
            for r in fresh:
                last = r["seq"]
                await resp.write(("data: " + json.dumps(r, ensure_ascii=False) + "\n\n").encode("utf-8"))
            if not fresh:
                await resp.write(b": keepalive\n\n")
            await asyncio.sleep(0.5)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


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
    body, err = await raw_json_body(request)
    if err:
        return err
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


async def handle_api_capabilities(request: web.Request) -> web.Response:
    """Окружения и их инструменты: что ассистентка реально умеет."""
    if ctx.registry is None:
        return web.json_response({"environments": [], "errors": {}})
    return web.json_response({"environments": ctx.registry.snapshot(), "errors": ctx.registry.load_errors})


async def handle_api_modes(request: web.Request) -> web.Response:
    """Граф профилей: состояния, переходы, подграфы (живые - от модулей профиля)."""
    if ctx.profiles is None:
        return web.json_response({"nodes": [], "edges": [], "current": None})
    g = await ctx.profiles.graph()
    g["flags"] = {"streamer": bool(ctx.settings.load().get("streamer"))}   # стример: не профиль, а переключатель состояния
    return web.json_response(g)


async def handle_api_mode_post(request: web.Request) -> web.Response:
    """Ручной выбор профиля: {"mode": "rimworld"} или {"mode": "auto"} (пишет настройку profile)."""
    body, err = await raw_json_body(request)
    if err:
        return err
    if ctx.profiles is None:
        return web.json_response({"error": "режимы не подключены"}, status=503)
    problem = ctx.profiles.set_override(body.get("mode"))
    if problem:
        return web.json_response({"error": problem}, status=400)
    return web.json_response({"current": ctx.profiles.current_id, "override": ctx.profiles.override})


async def handle_api_menu_click(request: web.Request) -> web.Response:
    """Нажать кнопку главного меню RimWorld без Claude и без вопроса: {"text": "Быстрый запуск"}. Прежний HTTP-путь
    для модуля RimWorld сохранён, но работа идёт инструментом desktop.menu_click (белый список кнопок и правило простоя
    живут в модуле desktop)."""
    body, err = await raw_json_body(request)
    if err:
        return err
    if ctx.router is None:
        return web.json_response({"ok": False, "result": "ядро ещё не готово"}, status=503)
    ok, result, code = await ctx.router.run_tool_ex("desktop.menu_click", {"text": str(body.get("text") or "").strip()})
    if not ok and code == "denied":
        return web.json_response({"ok": False, "result": result}, status=403)
    return web.json_response({"ok": ok, "result": result})


async def handle_api_appraisal_label(request: web.Request) -> web.Response:
    """Отметка игрока по оценке события: пересылается модулю игры, у которого журнал (по HTTP: это пульт, не шина модулей)."""
    body, err = await raw_json_body(request)
    if err:
        return err
    import httpx
    base = presenters.rimworld_url()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{base}/api/appraisal_label", json=body)
            return web.json_response(r.json(), status=r.status_code)
    except Exception as e:
        return web.json_response({"saved": False, "error": str(e)}, status=502)


async def handle_api_character_post(request: web.Request) -> web.Response:
    """Смена персонажа (ТЗ 05, CHR-7): тело {"id": "..."}; память и речь переключаются ядром."""
    body, err = await raw_json_body(request)
    if err:
        return err
    if ctx.character is None:
        return web.json_response({"error": "персонажи ещё не готовы"}, status=503)
    try:
        return web.json_response(await ctx.character.switch(str(body.get("id") or "")))
    except character.CharacterError as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_api_settings_post(request: web.Request) -> web.Response:
    import settings
    body, err = await raw_json_body(request)
    if err:
        return err
    return web.json_response(ctx.settings.update(**{k: v for k, v in body.items()
                                                    if k in settings.DEFAULTS and k != "character"}))


ROUTES = [
    ("GET", "/", handle_root),
    ("GET", "/api/state", handle_api_state),
    ("POST", "/api/settings", handle_api_settings_post),
    ("POST", "/api/character", handle_api_character_post),
    ("GET", "/api/events", handle_api_events),
    ("GET", "/api/latency", handle_api_latency),
    ("GET", "/api/flow", handle_api_flow),
    ("GET", "/api/flow/stream", handle_api_flow_stream),
    ("GET", "/api/flow/node/{node}", handle_api_flow_node),
    ("GET", "/api/flow/turn/{turn}", handle_api_flow_turn),
    ("POST", "/api/rating", handle_api_rating),
    ("GET", "/api/decisions", handle_api_decisions),
    ("GET", "/api/capabilities", handle_api_capabilities),
    ("GET", "/api/modes", handle_api_modes),
    ("POST", "/api/appraisal_label", handle_api_appraisal_label),
    ("POST", "/api/menu_click", handle_api_menu_click),
    ("POST", "/api/mode", handle_api_mode_post),
]
