"""HTTP-маршруты раздела «Справочник» вкладки «Память» (ТЗ 04, KB-22).

GET    /api/knowledge/collections        коллекции: название, область, вид, документов, кусков, время обновления; настройки поиска
GET    /api/knowledge/search?q=&c=       пробный поиск (c - коллекции через запятую; пусто - все): score, cos, bm25_rank
DELETE /api/knowledge/doc/{uri}?c=       удалить документ коллекции c (все его куски)
DELETE /api/knowledge/collection/{name}  удалить коллекцию справочника
Опыт (`<область>.experience`) показывается, удаляется в блоке уроков. Базу ставит ядро (`set_kb`); без неё ответы пустые.
Удаление требует запроса со своей же страницы (заголовок Origin, если он есть, только локальный)."""
from aiohttp import web

_KB = None


def set_kb(kb) -> None:
    global _KB
    _KB = kb


def _local(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    return not origin or origin.startswith(("http://localhost:", "http://127.0.0.1:"))


async def handle_collections(request: web.Request) -> web.Response:
    if _KB is None:
        return web.json_response({"collections": [], "settings": {}})
    cfg = _KB.cfg()
    return web.json_response({"collections": await _KB.overview(),
                              "settings": {k: cfg[k] for k in ("min_cos", "min_cos_bm25", "experience_boost", "translate")},
                              "chat": _KB.chat_enabled()})


async def handle_search(request: web.Request) -> web.Response:
    query = request.query.get("q", "").strip()
    if _KB is None or not query:
        return web.json_response({"hits": []})
    cols = [c.strip() for c in request.query.get("c", "").split(",") if c.strip()]
    return web.json_response({"hits": await _KB.probe(query[:300], cols or None, 8)})


async def handle_delete_doc(request: web.Request) -> web.Response:
    if _KB is None or not _local(request):
        return web.json_response({"ok": False, "error": "недоступно"}, status=403)
    collection = request.query.get("c", "").strip()
    if not collection:
        return web.json_response({"ok": False, "error": "не указана коллекция"}, status=400)
    try:
        removed = await _KB.delete_doc(collection, request.match_info["id"])
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)
    return web.json_response({"ok": True, "removed": removed})


async def handle_delete_collection(request: web.Request) -> web.Response:
    if _KB is None or not _local(request):
        return web.json_response({"ok": False, "error": "недоступно"}, status=403)
    try:
        removed = await _KB.delete_collection(request.match_info["id"])
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=409)
    return web.json_response({"ok": True, "removed": removed})


def add_routes(app: web.Application) -> None:
    app.router.add_get("/api/knowledge/collections", handle_collections)
    app.router.add_get("/api/knowledge/search", handle_search)
    app.router.add_delete("/api/knowledge/doc/{id:.+}", handle_delete_doc)
    app.router.add_delete("/api/knowledge/collection/{id:.+}", handle_delete_collection)
