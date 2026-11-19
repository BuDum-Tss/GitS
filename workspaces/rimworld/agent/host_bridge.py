"""Мост между кодом модуля RimWorld и тем, что работает только на хосте Windows (docs/RIMWORLD_CONTAINER.md).

Контроллер, `main.py` и инструменты зовут только `HostBridge`; что стоит за ним, выбирает `make_host_bridge()` по
GITS_RIMWORLD_HOST_URL:
  пусто  -> `LocalHostBridge`: модуль на хосте, вызовы идут прямо в `game_launcher`, `game_watchdog`, `game_ui`, `run_recorder`
            (их логика не менялась; Windows-пакеты импортируются лениво, при первом вызове);
  задан  -> `RemoteHostBridge`: контейнер, вызовы идут HTTP-запросами к хостовому помощнику (`workspaces/rimworld/host`).

Все методы `HostBridge` асинхронные. Из рабочих потоков (`asyncio.to_thread`) зови их через `bridge.blocking(coro)`.
Объявления пользователю (`announce_self`, `announce_fact`) синхронные и идут из процесса модуля через `game_launcher`
(протокол событий и уведомитель `set_notifier`); объявления самого помощника доходят до того же уведомителя событиями `/events`.

Windows-пакеты (pyautogui, win32*, psutil, soundcard) здесь не импортируются: в режиме Remote их в образе нет.
"""
import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from collections.abc import Callable

from gits_sdk.envcompat import env

logger = logging.getLogger("host_bridge")

DEFAULT_TIMEOUT_S = 15.0
EVENTS_WAIT_S = 25.0
POLL_BACKOFF_MAX_S = 30.0
FINISH_TIMEOUT_S = {"stop": 60.0, "handoff": 180.0, "montage": 7200.0}
DONE_KEEP = 8                      # сколько уже завершённых записей помощник помнит (повторный `montage` после отказа монтажа)


class HostBridgeError(RuntimeError):
    """Помощник или хостовая часть ответили отказом. `code` короткий (unauthorized, bad_request, window_not_found, launch_failed...),
    `status` HTTP-статус (0 для локального режима)."""

    def __init__(self, message: str, code: str = "error", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class HostUnavailable(HostBridgeError):
    """Помощник не отвечает (не запущен, неверный адрес, таймаут): модуль продолжает работать, вызов даёт эту ошибку."""

    def __init__(self, message: str):
        super().__init__(message, code="unavailable", status=0)


class HostBridge:
    """Интерфейс. Реализации: `LocalHostBridge`, `RemoteHostBridge`."""

    is_remote = False

    # ── жизненный цикл ──
    def start_events(self) -> None:
        """Начать получать события помощника (в локальном режиме ничего не делает). Вызывать из работающего цикла событий."""

    async def close(self) -> None:
        """Остановить фоновый опрос событий."""

    def blocking(self, coro):
        """Выполнить сопрограмму моста из рабочего потока (не из потока цикла событий)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(1) as pool:                # вызвали из потока цикла событий: считаем в отдельном потоке
            return pool.submit(asyncio.run, coro).result()

    # ── объявления пользователю (синхронные, модульная сторона) ──
    def set_notifier(self, fn: Callable[[str], bool] | None) -> None:
        """Уведомитель: получает текст-факт, возвращает True, если передал его ядру (REC-7). Им же доставляются объявления помощника."""
        import game_launcher
        game_launcher.set_notifier(fn)

    def announce_self(self, fact: str, kind: str = "game") -> str:
        import game_launcher
        return game_launcher.announce_self(fact, kind)

    def announce_fact(self, text: str, kind: str | None = "game") -> str:
        import game_launcher
        return game_launcher.announce_fact(text, kind)

    # ── игра ──
    async def game_status(self) -> dict:
        """{running, mod_ready, window: missing|minimized|background|front, user_idle_s, launch_forbidden}."""
        raise NotImplementedError

    async def launch_game(self, reason: str, wait_seconds: float = 0.0, windowed: bool = True) -> str:
        raise NotImplementedError

    async def stop_game(self, graceful_s: float = 10.0) -> dict:
        """Закрыть игру: мягко (окно, до `graceful_s` с), затем жёстко. {"stopped": True, "how": "graceful"|"killed"|"not_running"}."""
        raise NotImplementedError

    async def press_quick_start(self) -> bool:
        raise NotImplementedError

    async def show_window(self) -> None:
        raise NotImplementedError

    # ── UI окна RimWorld ──
    async def ui_screenshot(self) -> bytes:
        raise NotImplementedError

    async def ui_window_size(self) -> tuple[int, int]:
        raise NotImplementedError

    async def ui_click(self, x: int, y: int, button: str = "left", clicks: int = 1) -> None:
        raise NotImplementedError

    async def ui_move(self, x: int, y: int) -> None:
        raise NotImplementedError

    async def ui_drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        raise NotImplementedError

    async def ui_type(self, text: str) -> None:
        raise NotImplementedError

    async def ui_key(self, key: str) -> None:
        raise NotImplementedError

    async def ui_scroll(self, x: int, y: int, amount: int) -> None:
        raise NotImplementedError

    # ── запись забега ──
    def make_recorder(self, memory_dir: str, settings_get: Callable[[], dict], max_hours: Callable[[], float] | None = None):
        """Объект с интерфейсом `RunRecorder` для контроллера (start, mark, close, produce, produce_for_montage, process,
        ensure_alive, ensure_audio, overdue, stop_blocking, current)."""
        raise NotImplementedError

    async def record_start(self, run_id: str, colony: str, why: str = "") -> dict | None:
        """Начать запись; статус записи (см. `_recording_status`) или None, если не началась (нет ffmpeg, запись выключена)."""
        raise NotImplementedError

    async def record_status(self) -> dict | None:
        raise NotImplementedError

    async def record_ensure_alive(self, max_hours: float | None = None) -> dict:
        """{"closed_id": id закрытого куска | None, "status": статус текущей записи | None}."""
        raise NotImplementedError

    async def record_ensure_audio(self) -> bool:
        raise NotImplementedError

    async def record_finish(self, record_id: str | None, outcome: str, mode: str, markers: list | None = None,
                            goal: dict | None = None, events: list | None = None) -> dict | None:
        """Закрыть запись и остановить захват. mode: `stop` (только остановка), `handoff` (сценарий и описание для модуля
        монтажа), `montage` (собственный монтаж). Результат режима или None (запись пустая или короткая)."""
        raise NotImplementedError


def _recording_status(rec) -> dict | None:
    """Статус записи `Recording` для помощника и для зеркала в контейнере."""
    if rec is None:
        return None
    audio = rec.audio
    return {"id": getattr(rec, "remote_id", None), "run_id": rec.run_id, "colony": rec.colony, "raw": str(rec.raw),
            "started": rec.started, "video_alive": rec.proc is not None and rec.proc.poll() is None,
            "audio_present": audio is not None, "audio_ok": bool(audio and audio.ok),
            "audio_stopping": bool(audio and audio.stopping), "audio_error": (audio.error if audio else "") or "",
            "has_sound": bool(rec.has_sound())}


class LocalHostBridge(HostBridge):
    """Хостовые части в этом же процессе. Модули ищутся в момент вызова (подмена в тестах), Windows-пакеты тянутся лениво."""

    def __init__(self):
        self._recorder = None
        self._max_hours: float | None = None
        self._pending: dict[str, object] = {}              # закрытые, но ещё не остановленные записи (id -> Recording)
        self._done: OrderedDict[str, object] = OrderedDict()

    # игра
    def _status_sync(self) -> dict:
        import game_launcher
        import game_watchdog
        running = game_launcher.is_running()
        return {"running": running, "mod_ready": bool(running and game_launcher.mod_ready()),
                "window": game_watchdog.window_state() if running else "missing",
                "user_idle_s": game_watchdog.user_idle_seconds(), "launch_forbidden": game_launcher.launch_forbidden()}

    async def game_status(self) -> dict:
        return await asyncio.to_thread(self._status_sync)

    async def launch_game(self, reason: str, wait_seconds: float = 0.0, windowed: bool = True) -> str:
        import game_launcher
        args = game_launcher.windowed_args() if windowed else ()      # всегда окно: полноэкранная игра сворачивается и слепнет
        try:
            return await asyncio.to_thread(game_launcher.launch, reason, wait_seconds=wait_seconds, args=args)
        except ValueError as e:
            raise HostBridgeError(str(e), code="bad_request") from e
        except RuntimeError as e:
            raise HostBridgeError(str(e), code="launch_failed") from e

    async def stop_game(self, graceful_s: float = 10.0) -> dict:
        import game_launcher
        try:
            how = await asyncio.to_thread(game_launcher.stop, graceful_s)
        except RuntimeError as e:
            raise HostBridgeError(str(e), code="stop_failed") from e
        return {"stopped": True, "how": how}

    async def press_quick_start(self) -> bool:
        import game_watchdog
        return bool(await asyncio.to_thread(game_watchdog.press_quick_start))

    async def show_window(self) -> None:
        import game_watchdog
        await self._ui(game_watchdog.show_window)

    # UI
    @staticmethod
    async def _ui(fn, *args, **kwargs):
        """Вызов game_ui в потоке; «окно не найдено» становится понятной ошибкой."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except RuntimeError as e:
            raise HostBridgeError(str(e), code="window_not_found") from e

    async def ui_screenshot(self) -> bytes:
        import game_ui
        return await self._ui(game_ui.screenshot_png)

    async def ui_window_size(self) -> tuple[int, int]:
        import game_ui
        return await self._ui(game_ui.window_size)

    async def ui_click(self, x, y, button="left", clicks=1) -> None:
        import game_ui
        await self._ui(game_ui.click, x, y, button=button, clicks=clicks)

    async def ui_move(self, x, y) -> None:
        import game_ui
        await self._ui(game_ui.move, x, y)

    async def ui_drag(self, x1, y1, x2, y2, button="left") -> None:
        import game_ui
        await self._ui(game_ui.drag, x1, y1, x2, y2, button=button)

    async def ui_type(self, text) -> None:
        import game_ui
        await self._ui(game_ui.type_text, text)

    async def ui_key(self, key) -> None:
        import game_ui
        await self._ui(game_ui.key_press, key)

    async def ui_scroll(self, x, y, amount) -> None:
        import game_ui
        await self._ui(game_ui.scroll, x, y, amount)

    # запись
    def make_recorder(self, memory_dir, settings_get, max_hours=None):
        from run_recorder import RunRecorder
        self._recorder = RunRecorder(memory_dir, settings_get, max_hours=max_hours)
        return self._recorder

    def _need_recorder(self):
        if self._recorder is None:
            raise HostBridgeError("запись забега не настроена (make_recorder не вызывали)", code="no_recorder")
        return self._recorder

    def _tag(self, rec):
        if rec is not None and not getattr(rec, "remote_id", None):
            rec.remote_id = uuid.uuid4().hex[:12]
        return rec

    async def record_start(self, run_id, colony, why=""):
        recorder = self._need_recorder()
        stale = recorder.current
        if stale is not None:                               # прежняя запись не закрыта: отложим её, новая должна начаться
            old = self._tag(recorder.close("superseded"))
            self._pending[old.remote_id] = old
        await asyncio.to_thread(recorder.start, run_id, colony, why or "забег начался")
        return _recording_status(self._tag(recorder.current))

    async def record_status(self):
        return _recording_status(self._tag(self._need_recorder().current))

    async def record_ensure_alive(self, max_hours=None):
        recorder = self._need_recorder()
        if max_hours is not None:
            self._max_hours = float(max_hours)
            recorder._max_hours = lambda: self._max_hours
        self._tag(recorder.current)
        old = await asyncio.to_thread(recorder.ensure_alive)
        closed_id = None
        if old is not None:
            self._tag(old)
            self._pending[old.remote_id] = old
            closed_id = old.remote_id
        return {"closed_id": closed_id, "status": _recording_status(self._tag(recorder.current))}

    async def record_ensure_audio(self):
        return bool(await asyncio.to_thread(self._need_recorder().ensure_audio))

    def _take(self, record_id):
        """Запись по id: ещё идущая, отложенная или уже завершённая. (запись, закрыта ли она уже)."""
        recorder = self._need_recorder()
        cur = recorder.current
        if cur is not None and record_id in (None, getattr(cur, "remote_id", None)):
            return recorder.close("stopped"), False
        if record_id in self._pending:
            return self._pending.pop(record_id), False
        if record_id in self._done:
            return self._done[record_id], True
        raise HostBridgeError(f"записи {record_id!r} нет (помощник перезапускался?)", code="unknown_record", status=404)

    async def record_finish(self, record_id, outcome, mode, markers=None, goal=None, events=None):
        if mode not in FINISH_TIMEOUT_S:
            raise HostBridgeError(f"неизвестный режим {mode!r}", code="bad_request", status=400)
        recorder = self._need_recorder()
        rec, finished = self._take(record_id)
        if not finished:
            self._tag(rec)
            if outcome:
                rec.outcome = outcome
            rec.markers = [(float(m[0]), str(m[1]), int(m[2])) for m in (markers or [])]
            if goal is not None:
                rec.goal = goal
            if events is not None:
                rec.events = events
            self._done[rec.remote_id] = rec
            while len(self._done) > DONE_KEEP:
                self._done.popitem(last=False)
        if mode == "stop":
            await asyncio.to_thread(recorder.stop_blocking, rec)
            return {"stopped": True}
        if mode == "handoff":
            return await recorder.produce_for_montage(rec)
        if finished:                                        # уже остановлена прежним handoff: только монтаж
            return await asyncio.to_thread(recorder.process, rec)
        return await recorder.produce(rec)


class RemoteHostBridge(HostBridge):
    """Хостовые части за HTTP хостового помощника (aiohttp). Токен в заголовке Authorization, в журнал не пишется."""

    is_remote = True

    def __init__(self, url: str, token: str, timeout: float = DEFAULT_TIMEOUT_S, events_wait: float = EVENTS_WAIT_S):
        if not url or not token:
            raise ValueError("RemoteHostBridge: нужны адрес (GITS_RIMWORLD_HOST_URL) и токен (GITS_RIMWORLD_HOST_TOKEN)")
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.events_wait = events_wait
        self._notifier: Callable[[str], bool] | None = None
        self.on_record_event: Callable[[dict], None] | None = None
        self._poller: asyncio.Task | None = None
        self.connected: bool | None = None            # последний исход связи: None - не пробовали
        self._last_error = ""

    def __repr__(self) -> str:                         # токен не попадает в журналы и трассировки
        return f"RemoteHostBridge({self.url})"

    # ── HTTP ──
    async def _request(self, method: str, path: str, body=None, timeout: float | None = None, raw: bool = False):
        import aiohttp
        headers = {"Authorization": f"Bearer {self._token}"}
        total = timeout if timeout is not None else self.timeout
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total)) as session:
                async with session.request(method, self.url + path, json=body, headers=headers) as resp:
                    data = await resp.read()
                    status = resp.status
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self._note_link(False, f"{type(e).__name__}: {e}")
            raise HostUnavailable(f"хостовый помощник {self.url} недоступен: {type(e).__name__}: {e}") from e
        self._note_link(True)
        if status == 401:
            raise HostBridgeError("помощник отклонил токен (проверь GITS_RIMWORLD_HOST_TOKEN)", code="unauthorized", status=401)
        if status >= 400:
            code, message = "error", f"HTTP {status}"
            try:
                import json
                parsed = json.loads(data.decode("utf-8", "replace"))
                code, message = str(parsed.get("code") or code), str(parsed.get("error") or message)
            except (ValueError, AttributeError):
                pass
            raise HostBridgeError(message, code=code, status=status)
        if raw:
            return data
        try:
            import json
            return json.loads(data.decode("utf-8")) if data else {}
        except ValueError as e:
            raise HostBridgeError("помощник ответил не JSON", code="bad_response", status=status) from e

    def _note_link(self, ok: bool, error: str = "") -> None:
        if ok and self.connected is False:
            logger.info("Хостовый помощник снова отвечает")
        if not ok and self._last_error != error:
            logger.warning(f"Хостовый помощник недоступен: {error}")
        self.connected = ok
        self._last_error = "" if ok else error

    async def _post(self, path: str, body: dict | None = None, timeout: float | None = None):
        return await self._request("POST", path, body or {}, timeout=timeout)

    # ── события ──
    def set_notifier(self, fn) -> None:
        self._notifier = fn
        super().set_notifier(fn)                       # модульная сторона объявлений работает через тот же уведомитель

    def start_events(self) -> None:
        if self._poller is None or self._poller.done():
            self._poller = asyncio.get_running_loop().create_task(self._poll_events(), name="host-events")

    async def close(self) -> None:
        poller, self._poller = self._poller, None
        if poller is not None:
            poller.cancel()
            await asyncio.gather(poller, return_exceptions=True)

    def dispatch(self, event: dict) -> None:
        """Событие помощника -> тот же обработчик, что в локальном режиме. Сбой обработчика не роняет опрос."""
        kind, data = event.get("type"), event.get("data") or {}
        try:
            if kind == "announce":
                if self._notifier is not None:
                    self._notifier(str(data.get("text") or ""))
            elif kind == "log":
                import events_log
                extra = {k: v for k, v in (data.get("data") or {}).items()}
                events_log.log(str(data.get("kind") or "video"), str(data.get("text") or ""), **extra)
            elif kind == "record":
                if self.on_record_event is not None:
                    self.on_record_event(data)
        except Exception as e:
            logger.warning(f"Обработчик события помощника {kind!r} упал: {e!r}")

    async def _poll_events(self) -> None:
        cursor: int | None = None
        epoch = None
        backoff = 1.0
        while True:
            try:
                if cursor is None:
                    health = await self._request("GET", "/health")
                    epoch, cursor = health.get("epoch"), int(health.get("events_last") or 0)
                data = await self._request("GET", f"/events?after={cursor}&wait={self.events_wait:g}",
                                           timeout=self.events_wait + self.timeout)
                if data.get("epoch") != epoch:             # помощник перезапустился: нумерация началась заново
                    epoch, cursor = data.get("epoch"), 0
                    continue
                for event in data.get("events") or []:
                    self.dispatch(event)
                    cursor = max(cursor, int(event.get("n") or 0))
                cursor = max(cursor, int(data.get("last") or 0)) if not data.get("events") else cursor
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except HostBridgeError as e:
                if e.code == "unauthorized":
                    logger.error(f"События помощника: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, POLL_BACKOFF_MAX_S)
            except Exception as e:                          # цикл опроса не должен умирать
                logger.error(f"Опрос событий помощника: {e!r}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, POLL_BACKOFF_MAX_S)

    # ── игра ──
    async def game_status(self) -> dict:
        return await self._request("GET", "/game/status")

    async def launch_game(self, reason, wait_seconds=0.0, windowed=True) -> str:
        data = await self._post("/game/launch", {"reason": reason, "wait_seconds": wait_seconds, "windowed": windowed},
                                timeout=self.timeout + wait_seconds)
        return str(data.get("result") or "")

    async def stop_game(self, graceful_s=10.0) -> dict:
        return await self._post("/game/stop", {"graceful_s": graceful_s}, timeout=self.timeout + graceful_s + 20)

    async def press_quick_start(self) -> bool:
        return bool((await self._post("/game/quick_start", timeout=60)).get("pressed"))

    async def show_window(self) -> None:
        await self._post("/game/show_window")

    # ── UI ──
    async def ui_screenshot(self) -> bytes:
        return await self._request("GET", "/ui/screenshot", raw=True, timeout=30)

    async def ui_window_size(self) -> tuple[int, int]:
        data = await self._request("GET", "/ui/window")
        return int(data["width"]), int(data["height"])

    async def ui_click(self, x, y, button="left", clicks=1) -> None:
        await self._post("/ui/click", {"x": x, "y": y, "button": button, "clicks": clicks})

    async def ui_move(self, x, y) -> None:
        await self._post("/ui/move", {"x": x, "y": y})

    async def ui_drag(self, x1, y1, x2, y2, button="left") -> None:
        await self._post("/ui/drag", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "button": button})

    async def ui_type(self, text) -> None:
        await self._post("/ui/type", {"text": text})

    async def ui_key(self, key) -> None:
        await self._post("/ui/key", {"key": key})

    async def ui_scroll(self, x, y, amount) -> None:
        await self._post("/ui/scroll", {"x": x, "y": y, "amount": amount})

    # ── запись ──
    def make_recorder(self, memory_dir, settings_get, max_hours=None):
        from remote_recorder import RemoteRunRecorder
        return RemoteRunRecorder(self, memory_dir, settings_get, max_hours=max_hours)

    async def record_start(self, run_id, colony, why=""):
        return (await self._post("/record/start", {"run_id": run_id, "colony": colony, "why": why})).get("status")

    async def record_status(self):
        return (await self._request("GET", "/record/status")).get("status")

    async def record_ensure_alive(self, max_hours=None):
        return await self._post("/record/ensure_alive", {"max_hours": max_hours})

    async def record_ensure_audio(self):
        return bool((await self._post("/record/ensure_audio")).get("restarted"))

    async def record_finish(self, record_id, outcome, mode, markers=None, goal=None, events=None):
        body = {"id": record_id, "outcome": outcome, "mode": mode, "markers": markers or [], "goal": goal, "events": events}
        return (await self._post("/record/finish", body, timeout=FINISH_TIMEOUT_S.get(mode, 180.0))).get("result")


class SyncUi:
    """Синхронный переходник с теми же функциями, что у `game_ui` (для инструментов MCP-сервера, которые вызываются в потоке)."""

    def __init__(self, bridge: HostBridge):
        self._bridge = bridge

    def screenshot_png(self) -> bytes:
        return self._bridge.blocking(self._bridge.ui_screenshot())

    def window_size(self) -> tuple[int, int]:
        return self._bridge.blocking(self._bridge.ui_window_size())

    def click(self, x, y, button="left", clicks=1):
        self._bridge.blocking(self._bridge.ui_click(x, y, button=button, clicks=clicks))

    def move(self, x, y):
        self._bridge.blocking(self._bridge.ui_move(x, y))

    def drag(self, x1, y1, x2, y2, button="left"):
        self._bridge.blocking(self._bridge.ui_drag(x1, y1, x2, y2, button=button))

    def type_text(self, text):
        self._bridge.blocking(self._bridge.ui_type(text))

    def key_press(self, key):
        self._bridge.blocking(self._bridge.ui_key(key))

    def scroll(self, x, y, amount):
        self._bridge.blocking(self._bridge.ui_scroll(x, y, amount))


def remote_configured() -> bool:
    return bool((env("GITS_RIMWORLD_HOST_URL") or "").strip())


def make_host_bridge() -> HostBridge:
    """GITS_RIMWORLD_HOST_URL пусто: `LocalHostBridge`; задан: `RemoteHostBridge` (токен GITS_RIMWORLD_HOST_TOKEN обязателен)."""
    url = (env("GITS_RIMWORLD_HOST_URL") or "").strip()
    if not url:
        return LocalHostBridge()
    token = (env("GITS_RIMWORLD_HOST_TOKEN") or "").strip()
    if not token:
        raise ValueError("GITS_RIMWORLD_HOST_URL задан, а GITS_RIMWORLD_HOST_TOKEN нет: без токена помощник не принимает запросы")
    return RemoteHostBridge(url, token)


def ui_facade():
    """Объект с функциями UI игры для синхронного кода: модуль `game_ui` на хосте или `SyncUi` через помощника в контейнере."""
    if remote_configured():
        return SyncUi(make_host_bridge())
    import game_ui
    return game_ui

