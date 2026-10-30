"""Роутер дашборда: внимание, фокус, эмоции, мысли, объявления (ТЗ 03, 18, 26, 30; ТЗ 29 ARC-8)."""
import asyncio
import re

from aiohttp import web

from .context import ctx, json_body

NOTIFY_TEXT_MAX = 300
_NOTIFY_SOURCE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_LOCAL_PEERS = ("127.0.0.1", "::1")


async def handle_api_thoughts(request: web.Request) -> web.Response:
    """Журнал мыслей (ТЗ 30, IMP-6) и ожидающие напоминания себе. Мысли не озвучиваются."""
    if ctx.impulses is None:
        return web.json_response({"thoughts": [], "reminders": []})
    try:
        n = max(1, min(200, int(request.query.get("n", 50))))
    except ValueError:
        n = 50
    thoughts = await asyncio.to_thread(ctx.impulses.journal.recent, n)
    reminders = await asyncio.to_thread(ctx.impulses.reminders.pending)
    return web.json_response({"thoughts": thoughts, "reminders": reminders})


async def handle_api_workspace(request: web.Request) -> web.Response:
    """Среда агента (ТЗ 32): где он сейчас, почему, цель, доступные среды и история переключений."""
    if ctx.workspace is None:
        return web.json_response({"current": None, "available": [], "history": []})
    return web.json_response(ctx.workspace.snapshot())


async def handle_api_workspace_enter(request: web.Request) -> web.Response:
    """{"workspace": "rimworld", "goal": "..."}: войти в среду по просьбе пользователя (причина user)."""
    body, err = await json_body(request)
    if err:
        return err
    if ctx.workspace is None:
        return web.json_response({"error": "среды не подключены"}, status=503)
    ws, goal = body.get("workspace"), body.get("goal") or ""
    if not isinstance(ws, str) or not ws or not isinstance(goal, str):
        return web.json_response({"error": "workspace: строка, goal: строка"}, status=400)
    res = await ctx.workspace.enter(ws, "user", goal[:200])
    return web.json_response(res, status=200 if res.get("ok") else 409)


async def handle_api_workspace_exit(request: web.Request) -> web.Response:
    """Вернуться в базовую среду (ассистент) по просьбе пользователя."""
    if ctx.workspace is None:
        return web.json_response({"error": "среды не подключены"}, status=503)
    res = await ctx.workspace.leave("user")
    return web.json_response(res, status=200 if res.get("ok") else 409)


async def handle_api_attention(request: web.Request) -> web.Response:
    """Правила, очередь речи, стек фокуса, сводка, последние решения и настройки внимания."""
    if ctx.attention is None:
        return web.json_response({"rules": [], "queue": [], "focus": [], "digest": {}, "decisions": [], "settings": {}})
    return web.json_response(ctx.attention.snapshot())


async def handle_api_attention_clear(request: web.Request) -> web.Response:
    """{"id": "r-xxxxxx"} снять правило; без id снять все."""
    body, err = await json_body(request)
    if err:
        return err
    if ctx.attention is None:
        return web.json_response({"error": "внимание не подключено"}, status=503)
    rule_id = body.get("id")
    if rule_id is not None and not isinstance(rule_id, str):
        return web.json_response({"error": "id должен быть строкой"}, status=400)
    return web.json_response({"ok": True, "text": await ctx.attention.clear_rules(rule_id)})


async def handle_api_notify(request: web.Request) -> web.Response:
    """Объявление от внешнего скрипта или теста (не модуль): `{"text": str до 300 символов, "source": "rimworld-tests"}`.
    Идёт как объявление о запуске (AN-2, `announce: true`) от `source`: мимо формулы важности, звучит всегда (коротко при
    «не отвлекать», в сводку, если пользователь отошёл); текст она произнесёт своими словами.
    Только с этого же компьютера и только JSON."""
    if request.remote not in _LOCAL_PEERS:
        return web.json_response({"error": "forbidden"}, status=403)
    body, err = await json_body(request)
    if err:
        return err
    text, source = body.get("text"), body.get("source")
    if not isinstance(text, str) or not text.strip() or len(text) > NOTIFY_TEXT_MAX:
        return web.json_response({"error": f"text: непустая строка не длиннее {NOTIFY_TEXT_MAX} символов"}, status=400)
    if not isinstance(source, str) or not _NOTIFY_SOURCE.match(source):
        return web.json_response({"error": "source: короткое имя из латиницы, цифр и _.-"}, status=400)
    if ctx.attention is None:
        return web.json_response({"error": "внимание не подключено"}, status=503)
    await ctx.attention.submit_speech(source, {"text": text.strip(), "mode": "fact", "urgency_hint": 3,
                                               "announce": True})
    return web.json_response({"ok": True})


ROUTES = [
    ("GET", "/api/attention", handle_api_attention),
    ("POST", "/api/attention/rules/clear", handle_api_attention_clear),
    ("GET", "/api/workspace", handle_api_workspace),
    ("POST", "/api/workspace/enter", handle_api_workspace_enter),
    ("POST", "/api/workspace/exit", handle_api_workspace_exit),
    ("GET", "/api/thoughts", handle_api_thoughts),


    ("POST", "/api/notify", handle_api_notify),
]
