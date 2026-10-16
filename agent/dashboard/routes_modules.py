"""Роутер дашборда: модули, пространство модуля, артефакты и задачи (ТЗ 02, ТЗ 15, ТЗ 29 ARC-8)."""
import asyncio

import module_panel
from aiohttp import web
from gits_sdk.protocol import JOB_ID_RE

from . import presenters
from .context import background, ctx, json_body

PANEL_ACTION_TIMEOUT_S = 15.0       # panel.action: ответ модуля не позже


def _module_or_404(request: web.Request):
    if ctx.modules is None:
        return None, web.json_response({"error": "модули не подключены"}, status=503)
    rec = ctx.modules.get(request.match_info["id"])
    if rec is None:
        return None, web.json_response({"error": "нет такого модуля"}, status=404)
    return rec, None


async def handle_api_modules(request: web.Request) -> web.Response:
    """Модули: состояние, инструменты, настройки (CORE-37). У каждой записи `kind`: module, service (компонент ядра) или
    planned (ещё не установлен); записи для разработчиков (echo) отдаются только при настройке dev_mode."""
    if ctx.modules is None:
        return web.json_response([])
    dev_mode = bool(await asyncio.to_thread(lambda: ctx.settings.load().get("dev_mode")))
    return web.json_response(ctx.modules.dashboard_rows(dev_mode))


async def handle_api_module_enabled(request: web.Request) -> web.Response:
    """{"enabled": bool}: включить или выключить модуль; процесс запускает или останавливает супервизор."""
    body, err = await json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    if not isinstance(body.get("enabled"), bool):
        return web.json_response({"error": "enabled должно быть true или false"}, status=400)
    await asyncio.to_thread(lambda: ctx.settings.update(modules={rec.id: {"enabled": body["enabled"]}}))
    ctx.modules.invalidate()
    if ctx.hub is not None:
        await ctx.hub.refresh()                               # модулю уходит тема module (MOD-14)
    if ctx.supervisor is not None and rec.id in ctx.supervisor.specs:
        background(ctx.supervisor.apply(rec.id))
    return web.json_response({"ok": True, "enabled": body["enabled"]})


async def handle_api_module_restart(request: web.Request) -> web.Response:
    body, err = await json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    if ctx.supervisor is None or rec.id not in ctx.supervisor.specs:
        return web.json_response({"error": "этот модуль запущен не ядром, перезапустить его можно только вручную"}, status=400)
    background(ctx.supervisor.restart(rec.id))
    return web.json_response({"ok": True})


async def handle_api_module_settings(request: web.Request) -> web.Response:
    """{"key": str, "value": any}: собственная настройка модуля по его settings_schema."""
    body, err = await json_body(request)
    if err:
        return err
    rec, err = _module_or_404(request)
    if err:
        return err
    schema = (rec.manifest or {}).get("settings_schema") or {}
    key = body.get("key")
    if key not in schema:
        return web.json_response({"error": f"у модуля нет настройки «{key}»"}, status=400)
    value, problem = presenters.check_setting_value(schema[key], body.get("value"))
    if problem:
        return web.json_response({"error": problem}, status=400)
    await asyncio.to_thread(lambda: ctx.settings.update(modules={rec.id: {key: value}}))
    ctx.modules.invalidate()
    if ctx.hub is not None:
        await ctx.hub.refresh()
    return web.json_response({"ok": True, "module_settings": ctx.modules.module_settings(rec.id)})


# ── Пространство модуля (TZ 15) ──────────────────────────────────────────────

async def handle_api_module_panel(request: web.Request) -> web.Response:
    """Пространство модуля: объявленное в манифесте `panel`, последние блоки `panel.set` и состояние подключения."""
    rec, err = _module_or_404(request)
    if err:
        return err
    return web.json_response(presenters.panel_view(rec))


async def handle_api_module_panel_action(request: web.Request) -> web.Response:
    """{"action_id": str, "args": {}}: кнопка или строка пространства. Идентификатор должен быть в panel.actions;
    модуль отвечает `panel.action`, адрес открытия проверяется (`module_panel.action_result`)."""
    body, err = await json_body(request)
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
    if ctx.hub is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = ctx.hub.connection(rec.id)
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
    body, err = await json_body(request)
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
    if ctx.hub is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = ctx.hub.connection(rec.id)
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
    if ctx.modules is None:
        return web.json_response({"error": "модули не подключены"}, status=503)
    q = request.query
    rec = ctx.modules.get(q.get("module", ""))
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


# ── Задачи ────────────────────────────────────────────────────────────────────

async def handle_api_jobs(request: web.Request) -> web.Response:
    """Задачи всех модулей."""
    return web.json_response(presenters.jobs_view(ctx.modules.jobs_all() if ctx.modules is not None else []))


async def handle_api_job_cancel(request: web.Request) -> web.Response:
    body, err = await json_body(request)
    if err:
        return err
    if ctx.hub is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    conn = ctx.hub.connection(request.match_info["module"])
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
    body, err = await json_body(request)
    if err:
        return err
    if ctx.hub is None:
        return web.json_response({"error": "шина модулей не подключена"}, status=503)
    problem = await ctx.hub.answer_job(request.match_info["module"], request.match_info["job_id"],
                                       str(body.get("question_id") or ""), body.get("answer"))
    if problem:
        return web.json_response({"error": problem}, status=400)
    return web.json_response({"ok": True})


ROUTES = [
    ("GET", "/api/modules", handle_api_modules),
    ("POST", "/api/modules/{id}/enabled", handle_api_module_enabled),
    ("POST", "/api/modules/{id}/restart", handle_api_module_restart),
    ("POST", "/api/modules/{id}/settings", handle_api_module_settings),
    ("GET", "/api/modules/{id}/panel", handle_api_module_panel),
    ("POST", "/api/modules/{id}/panel/action", handle_api_module_panel_action),
    ("POST", "/api/modules/{id}/panel/link", handle_api_module_panel_link),
    ("GET", "/api/artifact", handle_api_artifact),
    ("GET", "/api/jobs", handle_api_jobs),
    ("POST", "/api/jobs/{module}/{job_id}/cancel", handle_api_job_cancel),
    ("POST", "/api/jobs/{module}/{job_id}/answer", handle_api_job_answer),
]
