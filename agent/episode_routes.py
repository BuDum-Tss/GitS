"""HTTP-маршруты вкладки «Память» для эпизодов, дневника и сна (ТЗ 27, EPI-11).

GET  /api/episodes?limit=&offset=&status=&day=   список эпизодов (резюме, статус, версия)
GET  /api/episodes/{id}                          эпизод с событиями-источниками
GET  /api/diary                                  дневник и суточные сводки
GET  /api/sleep                                  статус сна и счётчики эпизодов
POST /api/sleep/now                              начать сон сейчас (режим scheduled, без бюджета)
POST /api/sleep/interrupt                        прервать сон
Сервис ставит ядро (`set_service`); без него ответы пустые."""
from aiohttp import web

from domain import episodes as dom

_SERVICE = None


def set_service(service) -> None:
    global _SERVICE
    _SERVICE = service


def _int(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(request.query.get(name, default))))
    except ValueError:
        return default


async def handle_episodes(request: web.Request) -> web.Response:
    if _SERVICE is None:
        return web.json_response({"episodes": [], "counts": {}})
    status = request.query.get("status") or None
    eps = await _SERVICE.recall.listing(limit=_int(request, "limit", 30, 1, 200), offset=_int(request, "offset", 0, 0, 100000),
                                        status=status if status in dom.STATUSES else None, day=request.query.get("day") or None)
    return web.json_response({"episodes": eps, "counts": _SERVICE.store.counts()})


async def handle_episode(request: web.Request) -> web.Response:
    ep = await _SERVICE.recall.expand(request.match_info["id"]) if _SERVICE is not None else None
    if ep is None:
        return web.json_response({"error": "нет такого эпизода"}, status=404)
    return web.json_response(ep)


async def handle_diary(request: web.Request) -> web.Response:
    if _SERVICE is None:
        return web.json_response({"diary": [], "digests": []})
    snap = await _SERVICE.snapshot(_int(request, "limit", 30, 1, 200))
    return web.json_response({"diary": snap["diary"], "digests": snap["digests"]})


async def handle_sleep(request: web.Request) -> web.Response:
    if _SERVICE is None or _SERVICE.scheduler is None:
        return web.json_response({"state": "off"})
    return web.json_response(_SERVICE.scheduler.status())


async def handle_sleep_now(request: web.Request) -> web.Response:
    if _SERVICE is None or _SERVICE.scheduler is None:
        return web.json_response({"ok": False, "error": "эпизоды не подключены"}, status=503)
    if _SERVICE.scheduler.state == "sleeping":
        return web.json_response({"ok": False, "error": "уже спит"})
    _SERVICE.scheduler.start(dom.SleepPlan("scheduled", None))
    return web.json_response({"ok": True})


async def handle_sleep_interrupt(request: web.Request) -> web.Response:
    if _SERVICE is not None and _SERVICE.scheduler is not None:
        _SERVICE.scheduler.interrupt("дашборд")
    return web.json_response({"ok": True})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/episodes", handle_episodes)
    app.router.add_get("/api/episodes/{id}", handle_episode)
    app.router.add_get("/api/diary", handle_diary)
    app.router.add_get("/api/sleep", handle_sleep)
    app.router.add_post("/api/sleep/now", handle_sleep_now)
    app.router.add_post("/api/sleep/interrupt", handle_sleep_interrupt)
