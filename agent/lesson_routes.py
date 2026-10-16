"""HTTP-маршруты блока «Уроки и убеждения» вкладки «Память» (ТЗ 28, LRN-10).

GET  /api/lessons                    уроки, активные убеждения, журнал изменений, доля повторов ошибок по видам, счётчик новых
POST /api/lessons/seen               отметить изменения просмотренными (снять уведомление)
POST /api/beliefs/{change}/revoke    отменить изменение убеждения: значение возвращается к прежнему
Хаб ставит ядро (`set_hub`); без него ответы пустые."""
from aiohttp import web

_HUB = None


def set_hub(hub) -> None:
    global _HUB
    _HUB = hub


async def handle_lessons(request: web.Request) -> web.Response:
    if _HUB is None:
        return web.json_response({"lessons": [], "beliefs": [], "changes": [], "repeat_rates": {}, "counts": {}, "unseen": 0})
    try:
        limit = max(1, min(200, int(request.query.get("limit", 30))))
    except ValueError:
        limit = 30
    return web.json_response(await _HUB.service().snapshot(limit))


async def handle_seen(request: web.Request) -> web.Response:
    if _HUB is not None:
        await _HUB.service().mark_seen()
    return web.json_response({"ok": True})


async def handle_revoke(request: web.Request) -> web.Response:
    if _HUB is None:
        return web.json_response({"ok": False, "error": "уроки не подключены"}, status=503)
    try:
        change = int(request.match_info["change"])
    except ValueError:
        return web.json_response({"ok": False, "error": "номер изменения"}, status=400)
    res = await _HUB.service().revoke(change, who="owner")
    return web.json_response(res, status=200 if res.get("ok") else 409)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/lessons", handle_lessons)
    app.router.add_post("/api/lessons/seen", handle_seen)
    app.router.add_post("/api/beliefs/{change}/revoke", handle_revoke)
