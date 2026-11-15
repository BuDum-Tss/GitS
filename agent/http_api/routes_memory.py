"""Роутер дашборда: разговор, память, разрешения (ТЗ 29, ARC-8)."""
from datetime import datetime

from services.conversation import approvals
from services.memory_service import episode_routes
from services.tracing import reply_trace
from services.tracing import tool_trace
from aiohttp import web
from services.tasks import model as task_dom

from . import presenters
from .context import ctx, raw_json_body
from .routes_system import read_json


async def memories_view() -> list[dict]:
    """Цели-планы (`plan`, задачи): единственное, что вкладка памяти берёт не из эпизодов."""
    items: list[dict] = []
    tasks = ctx.tasks() if ctx.tasks is not None else None
    if tasks is not None:
        try:
            items += [{"id": t.id, "kind": "plan", "text": t.goal, "ts": t.created, "due": t.due}
                      for t in await tasks.list_tasks("*", kinds=(task_dom.KIND_PLAN,))]
        except Exception:
            pass
    return items


_NOW_RANK = {task_dom.ACTIVE: 0, task_dom.PENDING: 1, task_dom.WAITING: 2, task_dom.BLOCKED: 3}


def _task_view(t) -> dict:
    return {"id": t.id, "goal": t.goal, "workspace": t.workspace, "status": t.status, "priority": t.priority,
            "steps": [s.to_dict() for s in t.steps], "progress": list(task_dom.progress(t))}


def now_tasks_view(found: list, workspace: str) -> dict:
    """Текущая задача агента по всем средам: идущая, затем ожидающие/отложенные по приоритету, сроку и свежести; остальные открытые отдельно."""
    items = [t for t in found if t.is_open and t.kind != task_dom.KIND_REMINDER]
    parents_open = {t.parent for t in items if t.parent}
    leaves = [t for t in items if t.id not in parents_open]
    leaves.sort(key=lambda t: (_NOW_RANK.get(t.status, 9), -t.priority, t.due if t.due is not None else float("inf"), -t.updated))
    act = leaves[0] if leaves else None
    rest = [t for t in leaves[1:]]
    return {"active": _task_view(act) if act else None,
            "open": [{k: v for k, v in _task_view(t).items() if k != "steps"} for t in rest], "workspace": workspace}


async def handle_api_tasks_now(request: web.Request) -> web.Response:
    """Вкладка «Состояние»: текущая задача и её шаги независимо от окружения."""
    tasks = ctx.tasks() if ctx.tasks is not None else None
    if tasks is None:
        return web.json_response({"active": None, "open": []})
    try:
        found = await tasks.list_tasks("*")
        return web.json_response(now_tasks_view(found, tasks.current_workspace()))
    except Exception:
        return web.json_response({"active": None, "open": []})


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
        memory = episode_routes.memory()
        return web.json_response({"removed": int(memory is not None and memory.forget(mem_id))})     # эпизод или заметка; канон не стирается
    text = str(body.get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty"}, status=400)
    memory = episode_routes.memory()
    return web.json_response({"removed": memory.forget_matching(text, strict=True) if memory is not None else 0})


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
    ("GET", "/api/tasks/now", handle_api_tasks_now),
    ("POST", "/api/approval", handle_api_approval),
    ("POST", "/api/memory/forget", handle_api_forget),
    ("GET", "/api/chat", handle_api_chat_get),
]
