"""Роутер дашборда: разговор, память, граф, разрешения (ТЗ 29, ARC-8)."""
from datetime import datetime

import approvals
import reply_trace
import tool_trace
from aiohttp import web
from domain import tasks as task_dom

from . import presenters
from .context import ctx, raw_json_body
from .routes_system import read_json


async def memories_view() -> list[dict]:
    """Что она помнит важного: профиль владельца (`user`) и цели-планы (`plan`, задачи)."""
    items: list[dict] = []
    if ctx.graph is not None:
        items += [{"id": f["uuid"], "kind": "user", "text": f["text"], "ts": f["ts"], "importance": f["importance"]}
                  for f in ctx.graph.profile_facts("owner", 100)]
    tasks = ctx.tasks() if ctx.tasks is not None else None
    if tasks is not None:
        try:
            items += [{"id": t.id, "kind": "plan", "text": t.goal, "ts": t.created, "due": t.due}
                      for t in await tasks.list_tasks("*", kinds=(task_dom.KIND_PLAN,))]
        except Exception:
            pass
    return items


async def handle_api_core(request: web.Request) -> web.Response:
    """Всё, что показывает UI помимо игры: разговор, зрение, память, вопрос о разрешении."""
    return web.json_response(presenters.core_payload(
        dialogue=read_json("dialogue.json", []), activity=read_json("activity.json", []),
        screen=presenters.screen_state(ctx.modules), memories=await memories_view(),
        relations=presenters.relations_view(read_json("relationships.json", {})), tools=tool_trace.recent(40),
        approval=presenters.approval_view(approvals.has_pending(), approvals.pending_text(), approvals.pending_info())))


async def handle_api_approval(request: web.Request) -> web.Response:
    body, err = await raw_json_body(request)
    if err:
        return err
    if "answer" in body:                      # choice: вариант, text: строка, approval: true/false
        return web.json_response({"resolved": approvals.answer(body["answer"])})
    return web.json_response({"resolved": approvals.resolve(bool(body.get("allow")))})


async def handle_api_forget(request: web.Request) -> web.Response:
    body, err = await raw_json_body(request)
    if err:
        return err
    if body.get("id"):
        mem_id = str(body["id"])
        tasks = ctx.tasks() if ctx.tasks is not None else None
        if mem_id.startswith("t-") and tasks is not None:                 # цель-план это задача: закрывается как отменённая
            try:
                await tasks.close(mem_id, status=task_dom.CANCELLED, note="забыто в дашборде")
                return web.json_response({"removed": 1})
            except Exception:
                return web.json_response({"removed": 0})
        return web.json_response({"removed": int(ctx.graph is not None and await ctx.graph.forget(mem_id))})
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    return web.json_response({"removed": ctx.graph.forget_matching(text, strict=True) if ctx.graph is not None else 0})


async def handle_api_graph(request: web.Request) -> web.Response:
    """Граф памяти: действующие факты, устаревшие и состояние (очередь, база)."""
    if ctx.graph is None:
        return web.json_response({"enabled": False, "status": "память-граф не подключена"})
    snap = await ctx.graph.snapshot()
    return web.json_response(presenters.graph_view(snap, status=ctx.graph.status_text(), pending=ctx.graph.pending(),
                                                   stats=await ctx.graph.stats()))


async def handle_api_graph_search(request: web.Request) -> web.Response:
    """Пробный поиск по фактам: что нашлось (с косинусом) и какой блок попадёт в контекст ответа."""
    query = request.query.get("q", "").strip()
    if ctx.graph is None or not query:
        return web.json_response({"query": query, "hits": [], "context": ""})
    return web.json_response(await ctx.graph.explain(query[:300]))


async def handle_api_graph_forget(request: web.Request) -> web.Response:
    body, err = await raw_json_body(request)
    if err:
        return err
    if ctx.graph is None or not body.get("uuid"):
        return web.json_response({"error": "нечего удалять"}, status=400)
    return web.json_response({"removed": await ctx.graph.forget(str(body["uuid"]))})


async def handle_api_chat_post(request: web.Request) -> web.Response:
    body, err = await raw_json_body(request)
    if err:
        return err
    message = (body.get("message") or "").strip()
    if not message:
        return web.json_response({"error": "empty message"}, status=400)

    ctx.chat_log.append({"text": message, "ts": datetime.now().isoformat()})
    del ctx.chat_log[:-50]

    if ctx.chat_callback:
        ctx.chat_callback(message)

    return web.json_response({"status": "ok"})


async def handle_api_chat_get(request: web.Request) -> web.Response:
    return web.json_response({"messages": ctx.chat_log[-20:]})


async def handle_api_reply_context(request: web.Request) -> web.Response:
    """Контекст реплики: запрос, по которому модель сформулировала фразу агента (кнопка «Контекст» в чате).
    `?text=` текст реплики из диалога. Нет записи (реплика без модели или после перезапуска): found=false."""
    entry = reply_trace.find(request.query.get("text", "")[:4000])
    if entry is None:
        return web.json_response({"found": False})
    return web.json_response({"found": True, **entry})


ROUTES = [
    ("GET", "/api/reply_context", handle_api_reply_context),
    ("POST", "/api/chat", handle_api_chat_post),
    ("GET", "/api/core", handle_api_core),
    ("POST", "/api/approval", handle_api_approval),
    ("POST", "/api/memory/forget", handle_api_forget),
    ("GET", "/api/graph", handle_api_graph),
    ("GET", "/api/graph/search", handle_api_graph_search),
    ("POST", "/api/graph/forget", handle_api_graph_forget),
    ("GET", "/api/chat", handle_api_chat_get),
]
