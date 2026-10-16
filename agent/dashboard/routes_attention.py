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


async def handle_api_affect(request: web.Request) -> web.Response:
    """Состояние эмоций (ТЗ 26, AFF-7): настроение, эмоции, график за 24 часа."""
    if ctx.affect is None:
        return web.json_response({"available": False})
    return web.json_response({"available": True, **ctx.affect.snapshot(with_history=True)})


async def handle_api_affect_reset(request: web.Request) -> web.Response:
    if ctx.affect is None:
        return web.json_response({"available": False})
    ctx.affect.reset()
    return web.json_response({"available": True, **ctx.affect.snapshot(with_history=True)})


async def handle_api_focus(request: web.Request) -> web.Response:
    """Фокус (ТЗ 18, FOC-9): кто в фокусе, очередь, checkpoint, журнал переходов, политики модулей."""
    if ctx.focus is None:
        return web.json_response({"holder": None, "queue": [], "checkpoints": [], "journal": [], "policies": {}})
    return web.json_response(ctx.focus.snapshot())


async def handle_api_focus_pin(request: web.Request) -> web.Response:
    """{"module": "rimworld"} закрепить фокус за модулем; {"module": null} снять закрепление."""
    body, err = await json_body(request)
    if err:
        return err
    if ctx.focus is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module = body.get("module")
    if module is not None and (not isinstance(module, str) or not module):
        return web.json_response({"error": "module: строка или null"}, status=400)
    return web.json_response(await ctx.focus.pin(module))


async def handle_api_focus_dnd(request: web.Request) -> web.Response:
    """{"module": "telegram" | "all", "on": true|false} - «не беспокоить» для модуля или для всего агента."""
    body, err = await json_body(request)
    if err:
        return err
    if ctx.focus is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module, on = body.get("module", "all"), body.get("on")
    if not isinstance(module, str) or not module or not isinstance(on, bool):
        return web.json_response({"error": "нужны module (строка) и on (true или false)"}, status=400)
    return web.json_response(await ctx.focus.set_dnd(module, on))


async def handle_api_focus_once(request: web.Request) -> web.Response:
    """{"module": "telegram"} - разовое исключение из «не беспокоить» для следующего обычного события модуля."""
    body, err = await json_body(request)
    if err:
        return err
    if ctx.focus is None:
        return web.json_response({"error": "фокус не подключён"}, status=503)
    module = body.get("module")
    if not isinstance(module, str) or not module:
        return web.json_response({"error": "нужен module (строка)"}, status=400)
    return web.json_response(await ctx.focus.allow_once(module))


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
    ("GET", "/api/focus", handle_api_focus),
    ("GET", "/api/affect", handle_api_affect),
    ("POST", "/api/affect/reset", handle_api_affect_reset),
    ("GET", "/api/thoughts", handle_api_thoughts),
    ("POST", "/api/focus/pin", handle_api_focus_pin),
    ("POST", "/api/focus/dnd", handle_api_focus_dnd),
    ("POST", "/api/focus/allow_once", handle_api_focus_once),
    ("POST", "/api/notify", handle_api_notify),
]
