"""Веб-дашборд ядра GitS — оверлей для стримера и пульт. Порт 8766.

ТЗ 29, ARC-8: пакет вместо одного файла. Роутеры по темам (`routes_system`, `routes_memory`, `routes_modules`,
`routes_attention`), общее состояние и защита запросов (`context`), презентеры ответов (`presenters`). Здесь только сборка
приложения, запуск, `set_*` для ядра и прежние имена (`dashboard.handle_api_*`, `_MEMORY_DIR`, `_screen_state`), чтобы
публичные адреса, ответы и импорты не менялись."""
import logging
import sys
import types
from pathlib import Path

from aiohttp import web

import episode_routes
import events_log
import settings
from envcompat import env

from . import presenters, routes_attention, routes_memory, routes_modules, routes_system
from .context import ctx
from .routes_attention import (  # noqa: F401  (прежние имена пакета)
    NOTIFY_TEXT_MAX, handle_api_affect, handle_api_affect_reset, handle_api_attention, handle_api_attention_clear,
    handle_api_focus, handle_api_focus_dnd, handle_api_focus_once, handle_api_focus_pin, handle_api_notify,
    handle_api_workspace, handle_api_workspace_enter, handle_api_workspace_exit,
    handle_api_thoughts)
from .routes_memory import (  # noqa: F401
    handle_api_approval, handle_api_chat_get, handle_api_chat_post, handle_api_core, handle_api_forget, handle_api_graph,
    handle_api_graph_forget)
from .routes_modules import (  # noqa: F401
    PANEL_ACTION_TIMEOUT_S, handle_api_artifact, handle_api_job_answer, handle_api_job_cancel, handle_api_jobs,
    handle_api_module_enabled, handle_api_module_panel, handle_api_module_panel_action, handle_api_module_panel_link,
    handle_api_module_restart, handle_api_module_settings, handle_api_modules)
from .routes_system import (  # noqa: F401
    handle_api_appraisal_label, handle_api_capabilities, handle_api_character_post, handle_api_decisions,
    handle_api_events, handle_api_latency, handle_api_menu_click, handle_api_mode_post, handle_api_modes,
    handle_api_rating, handle_api_settings_post, handle_api_state, handle_root)

logger = logging.getLogger("dashboard")

_ROUTERS = (routes_system, routes_memory, routes_modules, routes_attention)

_rimworld_url = presenters.rimworld_url


def _screen_state() -> dict | None:
    """Состояние модуля desktop для вкладки «Что она видит» (DESK-15): None, если модуль не подключён."""
    return presenters.screen_state(ctx.modules)


# ── Что ставит ядро (core_service) ──────────────────────────────────────────────────

def set_registry(registry):
    ctx.registry = registry


def set_modes(manager):
    ctx.profiles = manager


def set_router(router):
    ctx.router = router


def set_character(switch):
    ctx.character = switch


def set_graph(graph):
    ctx.graph = graph


def set_modules(modules, hub, supervisor=None):
    """Реестр модулей, шина и супервизор: ставит ядро."""
    ctx.modules, ctx.hub, ctx.supervisor = modules, hub, supervisor


def set_presence(presence):
    ctx.presence = presence


def set_attention(attention):
    ctx.attention = attention


def set_impulses(impulses):
    ctx.impulses = impulses


def set_focus(focus):
    ctx.focus = focus


def set_workspace(workspace) -> None:
    ctx.workspace = workspace


def set_affect(affect) -> None:
    ctx.affect = affect


# ── Server lifecycle ─────────────────────────────────────────────────────────

def build_app() -> web.Application:
    """Приложение дашборда: страницы, API и шина модулей (/module/ws)."""
    app = web.Application()
    for router in _ROUTERS:
        for method, path, handler in router.ROUTES:
            app.router.add_route(method, path, handler)
    episode_routes.add_routes(app)                      # ТЗ 27: эпизоды, дневник, сон
    if ctx.hub is not None:
        ctx.hub.attach(app)
    static_dir = Path(__file__).resolve().parents[1] / "static"
    if static_dir.exists():
        app.router.add_static("/static", static_dir)
    return app


async def start_dashboard(
    memory_dir: str = "./memory",
    port: int = 8766,
    chat_callback=None,
):
    ctx.memory_dir = Path(memory_dir)
    settings.init(memory_dir)
    events_log.init(memory_dir)
    ctx.chat_callback = chat_callback

    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, env("GITS_DASHBOARD_HOST", "127.0.0.1"), port)
    await site.start()
    logger.info(f"Дашборд запущен: http://localhost:{port}")


class _Facade(types.ModuleType):
    """Прежние изменяемые имена модуля (`dashboard._MEMORY_DIR = ...`, подмена `dashboard._is_local_json`) ведут в `ctx`."""

    @property
    def _MEMORY_DIR(self) -> Path:
        return ctx.memory_dir

    @_MEMORY_DIR.setter
    def _MEMORY_DIR(self, value) -> None:
        ctx.memory_dir = Path(value)

    @property
    def _is_local_json(self):
        return ctx.is_local_json

    @_is_local_json.setter
    def _is_local_json(self, func) -> None:
        ctx.is_local_json = func


sys.modules[__name__].__class__ = _Facade
