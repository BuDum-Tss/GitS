"""HTTP-сервер хостового помощника RimWorld (контракт: docs/RIMWORLD_CONTAINER.md, раздел «Контракт помощника»).

Оборачивает `HostBridge` (на хосте это `LocalHostBridge`): запуск игры, UI окна RimWorld, запись забега, события и прокси к моду.
Токен обязателен для всех путей, кроме `GET /health`; сравнение `hmac.compare_digest`, токен нигде не пишется в журнал.
"""
import asyncio
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from urllib.parse import quote

import aiohttp
from aiohttp import web
from yarl import URL

from host_bridge import HostBridgeError

logger = logging.getLogger("rimworld_host")

API_VERSION = 1
JSON_LIMIT = 64 * 1024                 # тело обычной команды
FINISH_LIMIT = 16 * 1024 * 1024        # закрытие записи несёт события забега
PROXY_BODY_LIMIT = 1024 * 1024         # тело запроса к моду
PROXY_RESPONSE_LIMIT = 8 * 1024 * 1024
PROXY_TIMEOUT_S = 30.0
EVENTS_KEEP = 500
EVENTS_MAX_WAIT_S = 25.0
MOD_DEFAULT = "http://localhost:8765"
MOD_FALLBACK = "http://[::1]:8765"
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")
STOP_GRACEFUL_MAX_S = 30             # сколько секунд помощник даёт игре закрыться мягко (дальше kill)
COORD_MAX = 20000
TEXT_MAX = 500
FORBIDDEN_KEYS = {"win", "winleft", "winright", "apps", "sleep", "printscreen", "prtsc", "prtscr", "command", "option"}
KEY_RE = re.compile(r"^[a-z0-9]{2,16}$")
BUTTONS = ("left", "right", "middle")
STATUS_BY_CODE = {"bad_request": 400, "unauthorized": 401, "unknown_record": 404, "window_not_found": 409,
                  "launch_failed": 409, "stop_failed": 409, "stop_busy": 409, "no_recorder": 503}


class EventJournal:
    """Журнал событий помощника в памяти: монотонные номера, ограниченная длина, ожидание новых (long-poll)."""

    def __init__(self, keep: int = EVENTS_KEEP):
        self.epoch = uuid.uuid4().hex[:12]
        self._events: deque = deque(maxlen=keep)
        self._n = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed: asyncio.Event | None = None
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._changed = asyncio.Event()

    @property
    def last(self) -> int:
        return self._n

    def add(self, kind: str, data: dict) -> int:
        """Добавить событие; можно из любого потока (объявления идут из рабочих потоков помощника)."""
        with self._lock:
            self._n += 1
            event = {"n": self._n, "ts": round(time.time(), 2), "type": kind, "data": data}
            self._events.append(event)
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake)
            except RuntimeError:
                pass
        return event["n"]

    def _wake(self) -> None:
        if self._changed is not None:
            self._changed.set()

    def after(self, n: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["n"] > n]

    @property
    def first(self) -> int:
        with self._lock:
            return self._events[0]["n"] if self._events else self._n + 1

    async def wait_after(self, n: int, wait: float) -> list[dict]:
        found = self.after(n)
        deadline = time.monotonic() + wait
        while not found and self._changed is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self._changed.clear()
            found = self.after(n)
            if found:
                break
            try:
                await asyncio.wait_for(self._changed.wait(), left)
            except asyncio.TimeoutError:
                break
            found = self.after(n)
        return found


def _json_error(status: int, message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _read_body(request: web.Request, limit: int) -> bytes:
    """Тело запроса не больше `limit` байт; больше: 413 (и без Content-Length тоже)."""
    if request.content_length is not None and request.content_length > limit:
        raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=request.content_length)
    data = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        data += chunk
        if len(data) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(data))
    return bytes(data)


async def _json_body(request: web.Request, limit: int = JSON_LIMIT) -> dict:
    raw = await _read_body(request, limit)
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HostBridgeError("тело запроса не JSON", code="bad_request", status=400)
    if not isinstance(body, dict):
        raise HostBridgeError("тело запроса должно быть объектом JSON", code="bad_request", status=400)
    return body


def _int(body: dict, name: str, lo: int = 0, hi: int = COORD_MAX, default: int | None = None) -> int:
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HostBridgeError(f"поле {name}: нужно целое число", code="bad_request", status=400)
    if not lo <= value <= hi:
        raise HostBridgeError(f"поле {name}: допустимо {lo}..{hi}", code="bad_request", status=400)
    return value


def _text(body: dict, name: str, limit: int = TEXT_MAX, required: bool = True) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise HostBridgeError(f"поле {name}: нужна непустая строка до {limit} символов", code="bad_request", status=400)
    return value


class HostApp:
    def __init__(self, bridge, token: str, rimworld_api: str | None = None, journal: EventJournal | None = None):
        if not token:
            raise ValueError("токен помощника обязателен")
        self.bridge = bridge
        self._token = token.encode("utf-8")
        self.journal = journal or EventJournal()
        base = (rimworld_api or MOD_DEFAULT).rstrip("/")
        self._stopping = False                             # закрытие игры уже идёт: второе не принимаем
        self.mod_targets = [base]
        if base == MOD_DEFAULT and MOD_FALLBACK not in self.mod_targets:
            self.mod_targets.append(MOD_FALLBACK)          # localhost мог резолвиться в 127.0.0.1, а мод слушает [::1]
        self.app = self._build()

    # ── служебное ──
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if request.path != "/health":
            header = request.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else ""
            if not given or not hmac.compare_digest(given.encode("utf-8"), self._token):
                return _json_error(401, "нужен заголовок Authorization: Bearer <токен>", "unauthorized")
        try:
            return await handler(request)
        except HostBridgeError as e:
            status = STATUS_BY_CODE.get(e.code) or e.status or 500
            return _json_error(status, str(e), e.code)
        except web.HTTPRequestEntityTooLarge:
            return _json_error(413, "тело запроса слишком большое", "too_large")
        except web.HTTPException:
            raise
        except Exception as e:                                # токен в сообщение не попадает: он только в заголовке
            logger.error(f"{request.method} {request.path}: {e!r}", exc_info=True)
            return _json_error(500, f"внутренняя ошибка: {type(e).__name__}", "internal")

    def _build(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=FINISH_LIMIT)
        app.on_startup.append(self._on_startup)
        r = app.router
        r.add_get("/health", self.health)
        r.add_get("/game/status", self.game_status)
        r.add_post("/game/launch", self.game_launch)
        r.add_post("/game/stop", self.game_stop)
        r.add_post("/game/quick_start", self.quick_start)
        r.add_post("/game/show_window", self.show_window)
        r.add_get("/ui/screenshot", self.ui_screenshot)
        r.add_get("/ui/window", self.ui_window)
        r.add_post("/ui/click", self.ui_click)
        r.add_post("/ui/move", self.ui_move)
        r.add_post("/ui/drag", self.ui_drag)
        r.add_post("/ui/type", self.ui_type)
        r.add_post("/ui/key", self.ui_key)
        r.add_post("/ui/scroll", self.ui_scroll)
        r.add_post("/record/start", self.record_start)
        r.add_get("/record/status", self.record_status)
        r.add_post("/record/ensure_alive", self.record_ensure_alive)
        r.add_post("/record/ensure_audio", self.record_ensure_audio)
        r.add_post("/record/finish", self.record_finish)
        r.add_get("/events", self.events)
        r.add_route("*", "/game-api/{tail:.*}", self.proxy)
        return app

    async def _on_startup(self, app: web.Application) -> None:
        self.journal.bind(asyncio.get_running_loop())

    # ── эндпоинты ──
    async def health(self, request):
        return web.json_response({"ok": True, "api": API_VERSION, "epoch": self.journal.epoch,
                                  "events_last": self.journal.last})

    async def game_status(self, request):
        status = dict(await self.bridge.game_status())
        try:
            status["recording"] = await self.bridge.record_status()
        except HostBridgeError:
            status["recording"] = None
        return web.json_response(status)

    async def game_launch(self, request):
        body = await _json_body(request)
        reason = _text(body, "reason")
        wait = body.get("wait_seconds", 0)
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= 180:
            raise HostBridgeError("поле wait_seconds: допустимо 0..180", code="bad_request", status=400)
        windowed = bool(body.get("windowed", True))
        result = await self.bridge.launch_game(reason, wait_seconds=float(wait), windowed=windowed)
        return web.json_response({"result": result})

    async def game_stop(self, request):
        """Закрыть игру: мягко (окно) в пределах graceful_s, иначе жёстко. Ответ {stopped, how: graceful|killed|not_running}."""
        body = await _json_body(request)
        graceful = body.get("graceful_s", 10)
        if isinstance(graceful, bool) or not isinstance(graceful, (int, float)) or not 0 <= graceful <= STOP_GRACEFUL_MAX_S:
            raise HostBridgeError(f"поле graceful_s: допустимо 0..{STOP_GRACEFUL_MAX_S}", code="bad_request", status=400)
        if self._stopping:
            raise HostBridgeError("закрытие игры уже идёт", code="stop_busy", status=409)
        self._stopping = True
        try:
            result = await self.bridge.stop_game(float(graceful))
        finally:
            self._stopping = False
        return web.json_response({"stopped": bool(result.get("stopped", True)), "how": str(result.get("how") or "")})

    async def quick_start(self, request):
        await _json_body(request)
        return web.json_response({"pressed": bool(await self.bridge.press_quick_start())})

    async def show_window(self, request):
        await _json_body(request)
        await self.bridge.show_window()
        return web.json_response({"ok": True})

    # UI: только окно RimWorld (game_ui находит его по заголовку); точки вне окна отклоняются
    async def _inside(self, *points: tuple[int, int]) -> None:
        width, height = await self.bridge.ui_window_size()
        for x, y in points:
            if x >= width or y >= height:
                raise HostBridgeError(f"точка ({x}, {y}) вне окна RimWorld {width}x{height}", code="bad_request", status=400)

    async def ui_screenshot(self, request):
        png = await self.bridge.ui_screenshot()
        return web.Response(body=png, content_type="image/png")

    async def ui_window(self, request):
        width, height = await self.bridge.ui_window_size()
        return web.json_response({"width": width, "height": height})

    @staticmethod
    def _button(body: dict) -> str:
        button = body.get("button", "left")
        if button not in BUTTONS:
            raise HostBridgeError(f"button: одно из {BUTTONS}", code="bad_request", status=400)
        return button

    async def ui_click(self, request):
        body = await _json_body(request)
        x, y, clicks = _int(body, "x"), _int(body, "y"), _int(body, "clicks", 1, 3, default=1)
        await self._inside((x, y))
        await self.bridge.ui_click(x, y, button=self._button(body), clicks=clicks)
        return web.json_response({"ok": True})

    async def ui_move(self, request):
        body = await _json_body(request)
        x, y = _int(body, "x"), _int(body, "y")
        await self._inside((x, y))
        await self.bridge.ui_move(x, y)
        return web.json_response({"ok": True})

    async def ui_drag(self, request):
        body = await _json_body(request)
        x1, y1, x2, y2 = (_int(body, n) for n in ("x1", "y1", "x2", "y2"))
        await self._inside((x1, y1), (x2, y2))
        await self.bridge.ui_drag(x1, y1, x2, y2, button=self._button(body))
        return web.json_response({"ok": True})

    async def ui_type(self, request):
        body = await _json_body(request)
        await self.bridge.ui_type(_text(body, "text"))
        return web.json_response({"ok": True})

    async def ui_key(self, request):
        body = await _json_body(request)
        key = _text(body, "key", 16).lower()
        if (len(key) != 1 and not KEY_RE.match(key)) or key in FORBIDDEN_KEYS:
            raise HostBridgeError(f"клавиша {key!r} не разрешена", code="bad_request", status=400)
        await self.bridge.ui_key(key)
        return web.json_response({"ok": True})

    async def ui_scroll(self, request):
        body = await _json_body(request)
        x, y, amount = _int(body, "x"), _int(body, "y"), _int(body, "amount", -100, 100)
        await self._inside((x, y))
        await self.bridge.ui_scroll(x, y, amount)
        return web.json_response({"ok": True})

    # запись забега
    async def record_start(self, request):
        body = await _json_body(request)
        status = await self.bridge.record_start(_text(body, "run_id", 120), _text(body, "colony", 200),
                                                _text(body, "why", 400, required=False))
        self.journal.add("record", {"op": "start", "started": status is not None, "status": status})
        return web.json_response({"status": status})

    async def record_status(self, request):
        return web.json_response({"status": await self.bridge.record_status()})

    async def record_ensure_alive(self, request):
        body = await _json_body(request)
        max_hours = body.get("max_hours")
        if max_hours is not None and (isinstance(max_hours, bool) or not isinstance(max_hours, (int, float)) or max_hours < 0):
            raise HostBridgeError("поле max_hours: неотрицательное число", code="bad_request", status=400)
        result = await self.bridge.record_ensure_alive(max_hours)
        if result.get("closed_id"):
            self.journal.add("record", {"op": "part_closed", "id": result["closed_id"]})
        return web.json_response(result)

    async def record_ensure_audio(self, request):
        await _json_body(request)
        restarted = bool(await self.bridge.record_ensure_audio())
        if restarted:
            self.journal.add("record", {"op": "audio_restarted"})
        return web.json_response({"restarted": restarted})

    async def record_finish(self, request):
        body = await _json_body(request, FINISH_LIMIT)
        mode = body.get("mode")
        if mode not in ("stop", "handoff", "montage"):
            raise HostBridgeError("поле mode: stop, handoff или montage", code="bad_request", status=400)
        record_id = body.get("id")
        if record_id is not None and not isinstance(record_id, str):
            raise HostBridgeError("поле id: строка", code="bad_request", status=400)
        markers, events, goal = body.get("markers") or [], body.get("events"), body.get("goal")
        if not isinstance(markers, list) or (events is not None and not isinstance(events, list)) or \
                (goal is not None and not isinstance(goal, dict)):
            raise HostBridgeError("markers и events: списки, goal: объект", code="bad_request", status=400)
        result = await self.bridge.record_finish(record_id, str(body.get("outcome") or ""), mode, markers, goal, events)
        self.journal.add("record", {"op": "finish", "id": record_id, "mode": mode, "done": result is not None})
        return web.json_response({"result": result})

    # события
    async def events(self, request):
        try:
            after = int(request.query.get("after", "0"))
            wait = float(request.query.get("wait", str(EVENTS_MAX_WAIT_S)))
        except ValueError:
            raise HostBridgeError("after и wait: числа", code="bad_request", status=400)
        wait = min(max(wait, 0.0), EVENTS_MAX_WAIT_S)
        events = await self.journal.wait_after(max(after, 0), wait) if wait > 0 else self.journal.after(max(after, 0))
        return web.json_response({"epoch": self.journal.epoch, "last": self.journal.last, "first": self.journal.first,
                                  "events": events})

    # прокси к моду: мод слушает только [::1] хоста, из контейнера он недоступен
    async def proxy(self, request):
        tail = request.match_info["tail"].lstrip("/")
        if not tail or ".." in tail.split("/") or "\\" in tail or "\x00" in tail:
            return _json_error(400, "путь к моду не задан или недопустим", "bad_request")
        if request.method not in ("GET", "POST"):
            return _json_error(405, "к моду допустимы GET и POST", "method_not_allowed")
        body = await _read_body(request, PROXY_BODY_LIMIT) if request.method == "POST" else None
        headers = {k: v for k in ("Content-Type", "Accept") if (v := request.headers.get(k))}
        last_error = "мод не отвечает"
        for base in self.mod_targets:
            url = f"{base}/{quote(tail, safe='/')}" + (f"?{request.query_string}" if request.query_string else "")
            target = URL(base)
            if (target.host or "") in LOCAL_NAMES:           # мод принимает в заголовке Host только имя localhost
                headers["Host"] = f"localhost:{target.port or 8765}"
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=PROXY_TIMEOUT_S)) as session:
                    async with session.request(request.method, url, data=body, headers=headers) as resp:
                        data = await resp.content.read(PROXY_RESPONSE_LIMIT + 1)
                        if len(data) > PROXY_RESPONSE_LIMIT:
                            return _json_error(502, "ответ мода слишком большой", "mod_response_too_large")
                        return web.Response(status=resp.status, body=data,
                                            content_type=resp.content_type or "application/octet-stream",
                                            charset=resp.charset)
            except asyncio.TimeoutError:
                return _json_error(504, "мод не ответил вовремя", "mod_timeout")
            except aiohttp.ClientConnectorError as e:
                last_error = f"мод не отвечает ({base}): {type(e).__name__}"
            except aiohttp.ClientError as e:
                return _json_error(502, f"сбой связи с модом: {type(e).__name__}", "mod_error")
        return _json_error(502, last_error, "mod_unreachable")


def wire_bridge(bridge, journal: EventJournal, mirror_log: bool = True):
    """Объявления помощника (`announce_*` в `game_launcher`) и протокол событий (`events_log.log`) уходят в журнал помощника:
    контейнер читает его через `GET /events` и отдаёт тем же обработчикам, что в локальном режиме. Возвращает функцию отката."""
    bridge.set_notifier(lambda text: bool(journal.add("announce", {"text": text})))
    restore = None
    if mirror_log:
        import events_log
        original = events_log.log

        def log(kind: str, text: str = "", **data):
            original(kind, text, **data)
            journal.add("log", {"kind": kind, "text": str(text)[:500], "data": json.loads(json.dumps(data, default=str))})
        events_log.log = log

        def restore():
            events_log.log = original
    return restore


def build_app(bridge, token: str, rimworld_api: str | None = None, journal: EventJournal | None = None,
              wire: bool = False, mirror_log: bool = True) -> web.Application:
    """Приложение aiohttp помощника. `wire=True`: подключить объявления и протокол к журналу событий (в `main.py`)."""
    host = HostApp(bridge, token, rimworld_api, journal)
    if wire:
        restore = wire_bridge(bridge, host.journal, mirror_log)
        if restore is not None:
            async def _restore(app):
                restore()
            host.app.on_cleanup.append(_restore)
    return host.app
