"""Общее состояние дашборда и защита запросов (ТЗ 29, ARC-8). Ядро кладёт сюда свои объекты через `set_*` пакета;
роутеры берут их из `ctx`. Настройки читаются через контракт `SettingsProvider` (адаптер `adapters/settings_provider.py`)."""
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from adapters.settings_provider import SettingsModule


def default_is_local_json(request: web.Request) -> bool:
    """Защита от чужих запросов: только JSON (браузерный cross-site запрос так не
    отправить без CORS-разрешения) и только со своей же страницы."""
    if request.content_type != "application/json":
        return False
    origin = request.headers.get("Origin")
    if origin and not origin.startswith(("http://localhost:", "http://127.0.0.1:")):
        return False
    return True


@dataclass
class DashboardContext:
    memory_dir: Path = Path("./memory")
    settings: Any = field(default_factory=SettingsModule)        # SettingsProvider
    is_local_json: Callable[[web.Request], bool] = default_is_local_json
    modules: Any = None            # ModuleRegistry
    hub: Any = None                # ModuleHub
    supervisor: Any = None         # Supervisor
    presence: Any = None           # PresenceTracker
    registry: Any = None           # реестр окружений
    profiles: Any = None           # ProfileManager
    router: Any = None             # Router (POST /api/menu_click)
    graph: Any = None              # GraphMemory
    character: Any = None          # CharacterSwitch
    attention: Any = None          # Attention
    focus: Any = None              # FocusManager
    workspace: Any = None          # WorkspaceManager (ТЗ 32)
    impulses: Any = None           # импульсы и размышление (ТЗ 30)
    affect: Any = None             # AffectService (ТЗ 26)
    chat_callback: Callable[[str], Any] | None = None
    chat_log: list = field(default_factory=list)                  # локальная история сообщений для окна чата
    bg_tasks: set = field(default_factory=set)


ctx = DashboardContext()


def background(coro) -> None:
    """Долгое дело (остановка процесса до 10 с) уходит в фон: кнопка в дашборде отвечает сразу."""
    task = asyncio.get_running_loop().create_task(coro)
    ctx.bg_tasks.add(task)
    task.add_done_callback(ctx.bg_tasks.discard)


async def json_body(request: web.Request):
    """Тело POST как объект или ответ-ошибка: (body, None) или (None, response)."""
    if not ctx.is_local_json(request):
        return None, web.json_response({"error": "forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return None, web.json_response({"error": "invalid json"}, status=400)
    if not isinstance(body, dict):
        return None, web.json_response({"error": "тело должно быть объектом"}, status=400)
    return body, None


async def raw_json_body(request: web.Request):
    """Как json_body, но тело может быть любым JSON (старые маршруты сами проверяют тип): (body, None) или (None, response)."""
    if not ctx.is_local_json(request):
        return None, web.json_response({"error": "forbidden"}, status=403)
    try:
        return await request.json(), None
    except Exception:
        return None, web.json_response({"error": "invalid json"}, status=400)
