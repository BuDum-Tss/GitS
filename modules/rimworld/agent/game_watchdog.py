"""Наблюдатель за игрой: следит, что RimWorld открыт, виден и подключён, и чинит это сам.

Раз в несколько секунд проверяет цепочку: игра запущена, мод отвечает, колония загружена (иначе создаёт поселение
через меню), окно не свёрнуто (а при захвате экрана ещё и не закрыто другим окном: иначе запись снимет чужое), запись видео и звука жива,
режим камеры в моде включён, игровое время идёт. Что не так, исправляет и пишет в протокол (вид `video`). Решение о
действиях вынесено в чистые функции `plan_actions` и `plan_time_actions`, чтобы их можно было проверить без игры.

Время (ТЗ 11c): RimWorld после высадки новой колонии ставит игру на паузу, и без снятия запись снимает стоячую картинку
(живой забег 2026-10-11: 5 часов «1 мартомай 5500 г., 6ч»). Паузу снимает команда мода `set_time_speed` со скоростью 1
(она ставит `CurTimeSpeed` напрямую; `resume` лишь переключает паузу и при паузе от окна может поставить её обратно).
"""
import asyncio
import logging
import ctypes

from envcompat import env
import time

import events_log

logger = logging.getLogger("game_watchdog")

INTERVAL_S = 15
LAUNCH_COOLDOWN_S = 180        # после запуска игры не пытаемся снова (загрузка идёт долго)
MENU_GRACE_S = 45              # в главном меню дольше этого: создаём поселение
FOCUS_COOLDOWN_S = 20          # как часто можно возвращать окно на передний план (это отвлекает пользователя)
WINDOW_TITLE = "RimWorld by Ludeon Studios"

STALL_UNPAUSE_DEFAULT_S = 180   # настройка модуля `stall_unpause_s` (60-1800): сколько тик может стоять, прежде чем снимать паузу
STALL_UNPAUSE_RANGE_S = (60, 1800)
USER_IDLE_MIN_S = 120           # пользователь «не за компьютером»: нет ввода столько секунд
USER_NOTE_EVERY_S = 600         # пауза при пользователе: запись в протокол не чаще раза в 10 минут
FRESH_COLONY_GRACE_S = 180      # сколько после входа в созданную колонию ждём паузу, которую ставит игра, чтобы её снять


def plan_actions(s: dict) -> list[str]:
    """По снимку состояния (`s`) решить, что чинить. Порядок важен: сначала игра, потом окно, потом запись."""
    acts = []
    if not s["game_running"]:
        # ТЗ 11e, POL-12: поднимать игру только когда модуль в фокусе (autostart_game или упала при фокусе), не по умолчанию
        return ["launch_game"] if s["launch_ok"] and s.get("launch_allowed") else []
    if not s["mod_ready"]:
        return []                                   # игра грузится: ждём, ничего не трогаем
    if s["window"] == "minimized" or (s["window"] == "background" and s["need_front"]):
        acts.append("show_window")
    if not s["in_game"]:
        if s["menu_for_s"] >= MENU_GRACE_S:
            acts.append("new_colony")
        return acts
    if s["recording_wanted"] and (s["recording"] == "dead" or s.get("recording_overdue")) and s["window"] == "front":
        acts.append("restart_recording")                # видео умерло или дошло до max_record_hours (REC-6)
    elif s["recording_wanted"] and s.get("audio_dead") and s["recording"] == "alive":
        acts.append("restart_audio")                    # оборвалась только запись звука (REC-2)
    if s["camera_wanted"] and s["camera_mode"] != "auto":
        acts.append("camera_mode")
    return acts


def stall_limit_s(value) -> float:
    """Настройка `stall_unpause_s` в допустимых пределах; мусор даёт значение по умолчанию."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(STALL_UNPAUSE_DEFAULT_S)
    return min(max(v, STALL_UNPAUSE_RANGE_S[0]), STALL_UNPAUSE_RANGE_S[1])


def track_tick(prev: tuple[int, float] | None, tick, now: float) -> tuple[tuple[int, float] | None, float]:
    """Следить за игровым тиком: `prev` это (последний тик, когда он в последний раз менялся). Возвращает новое значение
    и сколько секунд тик стоит. Тика нет (игра не в колонии): слежение сбрасывается."""
    if not isinstance(tick, (int, float)):
        return None, 0.0
    if prev is None or tick != prev[0]:
        return (tick, now), 0.0
    return prev, now - prev[1]


def plan_time_actions(t: dict) -> list[str]:
    """Решить, что делать со временем игры (ТЗ 11c). Поля `t`: in_game, paused, fresh_colony (колонию только что создал
    наблюдатель), time_wanted (запись забега или автозапуск игры), stalled_s (сколько тик стоит), stall_limit_s,
    planner_paused (паузу держит планирование постройки), user_idle_s, since_note_s (когда в последний раз писали в
    протокол про паузу при пользователе; None: ещё не писали), announced (про паузу при пользователе уже объявили).
    Действия: resume_new_colony, resume_stalled, note_user_paused, announce_user_paused."""
    if not t.get("in_game") or not t.get("paused"):
        return []
    if t.get("fresh_colony"):
        return ["resume_new_colony"]                  # паузу поставила сама игра при высадке: никто её не снимет
    if not t.get("time_wanted") or t.get("planner_paused"):
        return []
    if t.get("stalled_s", 0.0) < t.get("stall_limit_s", STALL_UNPAUSE_DEFAULT_S):
        return []
    if t.get("user_idle_s", 0.0) >= USER_IDLE_MIN_S:
        return ["resume_stalled"]
    acts = []
    since = t.get("since_note_s")
    if since is None or since >= USER_NOTE_EVERY_S:
        acts.append("note_user_paused")               # пользователь за компьютером: пауза может быть его, не трогаем
        if not t.get("announced"):
            acts.append("announce_user_paused")
    return acts


def user_idle_seconds() -> float:
    """Сколько секунд нет ввода с клавиатуры и мыши (GetLastInputInfo). Не удалось узнать: 0, то есть «пользователь рядом»."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        kernel = ctypes.windll.kernel32
        kernel.GetTickCount.restype = ctypes.c_uint
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((kernel.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


def window_state() -> str:
    """missing / minimized / background (закрыто другим окном) / front."""
    import win32gui
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    if not hwnd:
        return "missing"
    if win32gui.IsIconic(hwnd):
        return "minimized"
    return "front" if win32gui.GetForegroundWindow() == hwnd else "background"


def show_window():
    import game_ui
    game_ui._focus()


BUTTON_RGB = (106, 81, 46)       # цвет кнопок главного меню RimWorld
QUICK_START_REL = (0.743, 0.466)  # «Быстрый запуск (Dev)» в окне 1920x1080: (1426, 503)


def press_quick_start() -> bool:
    """Нажать кнопку «Быстрый запуск (Dev)» в главном меню (создаёт новое поселение). Координаты относительные;
    перед нажатием проверяется, что в этом месте кнопка (цвет), иначе ничего не делаем и возвращаем False."""
    import game_ui
    from PIL import Image
    import io
    show_window()
    w, h = game_ui.window_size()
    if w < 800:
        return False
    img = Image.open(io.BytesIO(game_ui.screenshot_png())).convert("RGB")
    sx, sy = img.size
    y = int(sy * QUICK_START_REL[1])
    # края кнопки без текста (слева и справа); подсветка под курсором меняет оттенок, поэтому допуск широкий
    edges = [img.getpixel((int(sx * fx), y)) for fx in (0.6915, 0.7935)]
    if not any(all(abs(a - b) <= 30 for a, b in zip(e, BUTTON_RGB, strict=True)) for e in edges):
        return False
    game_ui.click(int(w * QUICK_START_REL[0]), int(h * QUICK_START_REL[1]))
    return True


class GameWatchdog:
    """Хуки подставляет контроллер: он знает, как запускать игру, создавать колонию и слать команды камеры."""

    def __init__(self, *, settings_get, recorder, launch, new_colony, mod_ready, state_ready, send_camera,
                 on_camera_lost, get_state=None, send_command=None, module_setting=None, idle_seconds=None,
                 announce=None, clock=time.time, launch_allowed=None):
        self._launch_allowed = launch_allowed or (lambda: False)     # ТЗ 11e POL-12: без разрешения игру не поднимаем
        self._settings = settings_get
        self._recorder = recorder
        self._launch = launch
        self._new_colony = new_colony
        self._mod_ready = mod_ready
        self._state_ready = state_ready
        self._send_camera = send_camera            # (cmd: dict) -> str | None: ответ мода
        self._on_camera_lost = on_camera_lost
        self._get_state = get_state                  # () -> dict: состояние мода (tick, paused, time_speed); без него время не следим
        self._send_command = send_command            # (cmd: dict) -> bool: команда мода
        self._module_setting = module_setting or (lambda key, default: default)
        self._idle = idle_seconds or user_idle_seconds
        self._announce = announce
        self._clock = clock
        self._tick_seen: tuple[int, float] | None = None
        self._fresh_pending = False                   # колонию создал наблюдатель: после входа в неё снять паузу игры
        self._fresh_since: float | None = None
        self._last_user_note: float | None = None
        self._user_announced = False
        self._last_launch = 0.0
        self._last_focus = 0.0
        self._menu_since: float | None = None
        self.status: dict = {}
        self._last_notes: dict[str, float] = {}

    def _note(self, key: str, text: str, every: float = 300):
        """Запись в протокол не чаще раза в `every` секунд на одну тему: наблюдатель не должен его засорять."""
        now = self._clock()
        if now - self._last_notes.get(key, -1e12) >= every:
            self._last_notes[key] = now
            events_log.log("video", text)
            logger.info(text)

    def _time_snapshot(self, in_game: bool, recording_wanted_cfg: bool, now: float) -> dict:
        """Поля о ходе игрового времени для `plan_time_actions`."""
        import planning_pause
        state = self._get_state() if (in_game and self._get_state) else {}
        tick = state.get("tick") if isinstance(state, dict) else None
        self._tick_seen, stalled = track_tick(self._tick_seen if in_game else None, tick, now)
        if stalled == 0.0:
            self._user_announced = False
            self._last_user_note = None
        if self._fresh_pending and in_game:
            self._fresh_since = self._fresh_since or now
            if now - self._fresh_since > FRESH_COLONY_GRACE_S:
                self._fresh_pending = False
        fresh = bool(self._fresh_pending and in_game)
        limit = stall_limit_s(self._module_setting("stall_unpause_s", STALL_UNPAUSE_DEFAULT_S))
        return {
            "paused": bool(state.get("paused")) if isinstance(state, dict) else False,
            "time_speed": state.get("time_speed") if isinstance(state, dict) else None,
            "tick": tick, "stalled_s": stalled, "fresh_colony": fresh,
            "stall_limit_s": limit,
            "time_wanted": bool(recording_wanted_cfg),
            "planner_paused": planning_pause.active(),
            "user_idle_s": self._idle() if stalled >= limit else 0.0,
            "since_note_s": None if self._last_user_note is None else now - self._last_user_note,
            "announced": self._user_announced,
        }

    def snapshot(self) -> dict:
        import game_launcher
        now = self._clock()
        cfg = self._settings()
        recording_wanted = bool(cfg.get("record_runs"))
        running = game_launcher.is_running()
        mod = running and self._mod_ready()
        in_game = mod and self._state_ready()
        if mod and not in_game:
            self._menu_since = self._menu_since or now
        else:
            self._menu_since = None
        rec = self._recorder.current if self._recorder else None
        recording = "none" if rec is None else ("alive" if rec.proc is not None and rec.proc.poll() is None else "dead")
        cam = None
        if in_game and recording_wanted:
            try:
                import json
                cam = json.loads(self._send_camera({"type": "camera_state"}) or "{}").get("mode")
            except Exception:
                cam = None
        return {
            "game_running": running, "mod_ready": mod, "in_game": in_game,
            "launch_ok": now - self._last_launch >= LAUNCH_COOLDOWN_S,
            "launch_allowed": bool(self._launch_allowed()),
            "menu_for_s": now - self._menu_since if self._menu_since else 0.0,
            "window": window_state() if running else "missing",
            "need_front": recording_wanted and env("GITS_RECORD_SOURCE", "gfxcapture") != "gfxcapture",
            "recording_wanted": recording_wanted and in_game, "recording": recording,
            "audio": bool(rec and rec.audio and rec.audio.ok),
            "audio_dead": bool(rec and rec.audio and not rec.audio.ok and not rec.audio.stopping),
            "recording_overdue": bool(rec and self._recorder.overdue()),
            "camera_wanted": recording_wanted, "camera_mode": cam,
            **self._time_snapshot(bool(in_game), bool(cfg.get("record_runs")), now),
        }

    async def step(self) -> list[str]:
        s = await asyncio.to_thread(self.snapshot)
        self.status = {**s, "checked_at": time.time()}
        acts = plan_actions(s) + plan_time_actions(s)
        for act in acts:
            try:
                await self._do(act, s)
            except Exception as e:
                logger.error(f"Наблюдатель: {act}: {e}", exc_info=True)
        return acts

    async def _do(self, act: str, s: dict):
        if act == "launch_game":
            self._last_launch = time.time()
            self._note("launch", "RimWorld не запущена: запускаю", 60)
            await asyncio.to_thread(self._launch)
        elif act == "show_window":
            if time.time() - self._last_focus >= FOCUS_COOLDOWN_S:
                self._last_focus = time.time()
                self._note("window", f"окно RimWorld {s['window']}: возвращаю на передний план", 120)
                await asyncio.to_thread(show_window)
        elif act == "new_colony":
            self._note("colony", "игра в главном меню: создаю поселение", 120)
            await self._new_colony()
            self._fresh_pending, self._fresh_since = True, None          # GT-1: игра ставит новую колонию на паузу
        elif act == "restart_recording":
            self._note("rec", "запись видео остановилась или дошла до предела длины: перехожу к следующему куску", 60)
            old = await asyncio.to_thread(self._recorder.ensure_alive)
            if old is not None:
                asyncio.create_task(self._recorder.produce(old))
        elif act == "restart_audio":
            await asyncio.to_thread(self._recorder.ensure_audio)       # сам пишет в протокол и не чаще раза в 30 с
        elif act == "camera_mode":
            self._on_camera_lost()
        elif act == "resume_new_colony":
            self._fresh_pending = False
            await self._unpause(s, "Ты сама снимаешь паузу в игре (не пользователь): колония создана для записи забега")
        elif act == "resume_stalled":
            minutes = max(1, round(s["stalled_s"] / 60))
            await self._unpause(s, f"Ты сама снимаешь паузу в игре (не пользователь): игра стояла на паузе не меньше {minutes} мин, пользователя нет за компьютером")
        elif act == "note_user_paused":
            self._last_user_note = self._clock()
            self._note("user_paused", f"игра на паузе уже {round(s['stalled_s'] / 60)} мин, пользователь за компьютером: "
                       "паузу не трогаю, запись идёт впустую", 0)
        elif act == "announce_user_paused":
            self._user_announced = True
            await self._say("Игра на паузе, запись идёт впустую: пользователь за компьютером, ты паузу не снимаешь")

    async def _say(self, text: str):
        """Объявить факт пользователю через ядро (правило 14 ТЗ, REC-7)."""
        announce = self._announce
        if announce is None:
            import game_launcher
            announce = lambda fact: game_launcher.announce_fact(fact, None)       # в протокол пишет вызывающий
        await asyncio.to_thread(announce, text)

    async def _unpause(self, s: dict, text: str):
        """Объявить и снять паузу: скорость 1 (`set_time_speed`). Результат проверяем по состоянию мода."""
        events_log.log("video", text)
        await self._say(text)
        ok = await asyncio.to_thread(self._send_command, {"type": "set_time_speed", "speed": 1}) if self._send_command else False
        state = (await asyncio.to_thread(self._get_state) if self._get_state else None) or {}
        self._tick_seen = (state.get("tick", s.get("tick") or 0), self._clock())      # следующая проверка стоячего тика с нуля
        if ok and not state.get("paused"):
            events_log.log("video", f"пауза снята: скорость {state.get('time_speed')}")
            logger.info("Наблюдатель: пауза снята")
        else:
            self._note("unpause_failed", "пауза не снялась (игру держит окно или письмо?): проверю позже", 120)

    async def run(self):
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Ошибка наблюдателя: {e}", exc_info=True)
            await asyncio.sleep(INTERVAL_S)
