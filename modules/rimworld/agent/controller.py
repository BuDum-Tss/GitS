"""Основной цикл управления — 6 независимых контуров:

  1. Реагирование  (быстрый)  — _tick_loop, каждые POLL_INTERVAL_SECS
  2. Планирование  (средний)  — _planning_loop, каждые PLANNING_INTERVAL_SECS
  3. Долгосрочное планирование (медленный) — _long_term_planning_loop
  4. Рефлексия     (медленный) — _reflection_loop
  5. Самоулучшение (самый редкий) — _self_improvement_loop, правит свой же код
  6. Навигация по UI — разовые вызовы _navigate_to_game (главное меню,
     гибель колонии) через мышь/клавиатуру (game_ui.py)

Все запускаются как независимые asyncio-задачи. Быстрый контур (реагирование)
НИКОГДА не ждёт остальные — иначе долгий фоновый вызов LLM блокирует реакцию
на критическое событие. Фоновые контуры делят между собой отдельный лок,
чтобы не запускать несколько тяжёлых LLM-вызовов одновременно и не гонять
файлы памяти параллельно.
"""
import asyncio
import logging
import os

from envcompat import env
import time
from pathlib import Path

import httpx
from collections import deque

from memory import Memory
import events_log
import event_text
import game_errors
import gamenet
import settings
import statediff
from relationships import Relationships
import focus_checkpoint
import focus_mode
import focus_status
import policy_actions as pa
import policy_events
import policy_manager
import knowledge
import real_threats

logger = logging.getLogger("controller")
MEM_DIR = Path(__file__).parent / "memory"      # файлы модуля рядом с кодом (как memory/tasks.json в _tasks_block)
AFK_SLEEP_S = 30                # ТЗ 11e POL-11: вне фокуса фоновые контуры спят, токены не тратятся


def _policy_log(kind: str, text: str) -> None:
    events_log.log(kind, text)

PROACTIVE_TICK_INTERVAL   = 2500  # 1 игровой час (GenDate.TicksPerHour)
POLL_INTERVAL_SECS        = 2.0
TICKS_PER_HOUR            = 2500
PLANNING_INTERVAL_SECS    = 8 * 60          # тактические задачи — раз в 8 минут
LONG_TERM_INTERVAL_SECS   = 25 * 60         # стратегические цели — раз в 25 минут
REFLECTION_INTERVAL_SECS  = 40 * 60         # уроки — раз в 40 минут
SELF_IMPROVEMENT_INTERVAL_SECS = 2 * 60 * 60  # правки собственного кода — раз в 2 часа
_ASSISTANT_URL = env("GITS_CORE_HTTP_URL", "http://127.0.0.1:8766")
QUICK_START_ATTEMPTS = 3                  # сколько раз нажимать «Быстрый запуск», прежде чем идти другим путём
QUICK_START_WAIT_POLLS = 15               # после нажатия ждём загрузку поселения столько опросов по 2 с
NAVIGATION_TIMEOUT_SECS = 240               # сам проход меню через Claude по экрану: один снимок занимает 40+ с
MENU_STUCK_THRESHOLD_SECS = 90             # торчим в главном меню дольше — проходим сами через UI
_BASE                     = gamenet.base_url(8765)   # http://localhost:8765 (мод отвечает только на это имя)
_GAME_PORT                = 8765
RIMWORLD_EXE              = r"C:\GOG Games\RimWorld\RimWorldWin64.exe"

def _active_ambition() -> dict | None:
    """Активная главная цель игры из memory/goals.json (её пишет set_goal из MCP-сервера)."""
    import json
    mem = os.getenv("MEMORY_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
    try:
        goals = json.loads(open(os.path.join(mem, "goals.json"), encoding="utf-8").read())
    except Exception:
        return None
    found = [g for g in goals if g.get("category") == "ambition" and g.get("status") == "active"]
    return found[-1] if found else None


MENU_GOAL = ("Дойди до активной игры: на главном меню RimWorld сначала поищи кнопку 'Быстрый запуск (Dev)' "
             "(Quick Start) и жми её: это самый быстрый путь к новому поселению. Если её нет, нажми New colony, "
             "выбери любое подходящее место и стартуй. Если и этого нет, нажми Continue (последний сейв) или "
             "открой Load и выбери самый свежий.")

BATTLE_OVER = ("Бой окончен: врагов на карте больше нет. Наведи порядок: undraft_pawns всех, спаси и вылечи раненых "
               "(rescue_pawn, tend_pawn), собери трофеи (unforbid_item на оружии и броне врагов), вооружи и одень "
               "колонистов лучшим из найденного (get_items), потом возвращайся к развитию колонии.")


_GAME_TALK = ("напад", "налёт", "налет", "рейд", "враг", "атак", "сража", "защит", "спас", "убей", "убить",
              "захват", "пешк", "колонист", "поселен", "оружи", "пистолет", "винтовк", "ранен", "истека", "лечи",
              "ловуш", "турел", "стен", "кроват", "грядк", "постро", "строй", "исслед", "приоритет", "расписан",
              "график", "караван", "торговц", "вооруж", "в бой", "драф", "отбей", "убеж", "бункер")


def _game_command(cmd: dict) -> bool:
    """Прямая команда игре (пауза/снятие паузы), минуя MCP. True при успехе."""
    if not _game_port_open():
        return False
    try:
        r = httpx.post(f"{_BASE}/command", json=cmd, timeout=6)
        return r.status_code == 200 and r.json().get("status") != "error"
    except Exception:
        return False


def _game_port_open() -> bool:
    """Быстрая проверка, слушает ли игра порт (по обоим адресам петли). Без неё подключение
    к выключенной игре в Windows висит 2-4 секунды и тормозит весь процесс."""
    return gamenet.port_open(_GAME_PORT)


def _ping() -> bool:
    if not _game_port_open():
        return False
    try:
        return httpx.get(f"{_BASE}/ping", timeout=2).status_code == 200
    except Exception:
        return False


def _get_state() -> dict:
    if not _game_port_open():
        return {}
    try:
        return real_threats.filter_threats(httpx.get(f"{_BASE}/state", timeout=5).json())
    except Exception:
        return {}


def _get_events() -> list[dict]:
    if not _game_port_open():
        return []
    try:
        return httpx.get(f"{_BASE}/events", timeout=5).json()
    except Exception:
        return []


def _camera_command(cmd: dict) -> str | None:
    """Команда камеры в мод. Возвращает ответ мода (для camera_state) или None при сбое."""
    try:
        r = httpx.post(f"{_BASE}/command", json=cmd, timeout=4).json()
        return r.get("result") if r.get("status") == "ok" else None
    except Exception:
        return None


def _as_list(value) -> list:
    """Список из ответа ядра или файла: строка или число не разбираются по символам."""
    return value if isinstance(value, list) else []


def _state_ready(state: dict) -> bool:
    return bool(state) and state.get("status") not in ("no_game", "no_map", "initializing")


MAX_COMMENTARY_CHARS  = 220   # жёсткий потолок длины — подстраховка поверх промпта
REPEAT_SIMILARITY_MAX = 0.55  # выше — считаем повтором предыдущей реплики


_READ_ONLY_PREFIXES = ("get_", "look_at", "survey_", "analyze_", "search_", "describe_", "estimate_", "find_", "list_", "switch_mode",
                       "open_game_tab", "game_screenshot", "camera_", "web_search", "remember_", "note_decision", "set_goal",
                       "request_planning", "select_target", "ui_state")


class Controller:
    def __init__(self, config: dict):
        self._memory = Memory(config.get("memory_dir", "./memory"))
        events_log.init(config.get("memory_dir", "./memory"))
        self._rel = Relationships(config.get("memory_dir", "./memory"))
        self._chat_buffer: deque = deque(maxlen=50)
        self._last_proactive_tick = 0
        self._colony_started      = False
        # Быстрый контур (реагирование) НИКОГДА не ждёт этот лок — иначе долгий
        # фоновый вызов (планирование/цели/рефлексия) блокирует реакцию на рейд.
        # Лок только между тремя фоновыми контурами — чтобы не долбить LLM
        # и файлы памяти всеми тремя одновременно.
        self._background_lock     = asyncio.Lock()
        # Будим долгосрочное планирование сразу при получении первого состояния
        # колонии (новый забег/другой сейв) — не ждать до 25 минут по таймеру
        self._long_term_trigger    = asyncio.Event()
        self._menu_greeted        = False  # уже заметили главное меню в этом заходе (для автопрохода по меню)
        self._menu_since          = None   # время, когда впервые увидели главное меню в этом заходе
        self._navigating          = False  # идёт ли сейчас UI-навигация (не дублировать)
        self._just_loaded         = False  # колония только что загрузилась — нужна особая реакция
        self._last_commentary     = ""     # для защиты от дословных повторов
        self.recorder             = None   # RunRecorder: запись забега на видео (None — выключена)
        self.director             = None   # CameraDirector: куда смотреть камере (только пока идёт запись)
        self._seen_game_errors    = 0      # сколько ошибок игры из state.game_log уже записано в журнал
        self.watchdog             = None   # GameWatchdog: игра запущена, видна, подключена
        self.link                 = None   # CoreLink: речь, эмоции и события игры через ядро (модуль rimworld, ТЗ 11)
        self.on_phase_change      = None   # () -> None: игра загрузилась или забег закончился (модуль отправит состояние сразу)
        self._goal_status         = None   # статусы целей на прошлой проверке (id -> status): событие, когда цель закрыта
        self._goal_mtime          = 0.0
        self._turn_reason         = ""     # почему идёт ход (для режима и зрителя)
        self._plan_reason         = ""     # что разбудило планирование (сигнал или просьба самого агента)
        self._last_plan_wake      = 0.0    # когда сигнал в последний раз будил планирование
        self._pending_seen        = 0      # сколько чертежей висит и с какого момента (сигнал планирования)
        self._pending_since       = 0.0
        self._turn_task           = None   # идущий игровой ход (фоновая задача)
        self._idle_since          = 0.0    # с какого момента колонисты без дела (0: заняты)
        self._need_turn           = False  # прошлый ход оборвался: следующий нужен без долгого ожидания
        self._planning_wake       = asyncio.Event()   # цели поставлены: пора раскладывать их на задачи
        self._last_turn_end       = 0.0    # когда закончился последний ход: темп обычных ходов зависит от режима расхода
        self._refl_seen           = -1     # сколько решений было при прошлой рефлексии
        self._graph_sent          = 0.0    # когда состав колонии в последний раз ушёл ассистенту (для графа памяти)
        self._prev_snap           = None   # снимок прошлого цикла: из разницы важное идёт в протокол событий
        self._turn_snap           = None   # снимок на начало её прошлого хода: из разницы «что изменилось»
        self._had_threats         = False  # были враги на карте: когда исчезнут, наступает «бой окончен»
        self._turn_kind           = ""
        self._turn_started        = 0.0
        # ТЗ 11e: политики, правила наблюдения, бюджеты боя, ручное управление, скорость, checkpoint фокуса
        self.policies             = policy_manager.PolicyManager(log=_policy_log)
        self._tick_now: int | None = None
        self._pending_events: deque = deque(maxlen=12)     # события с прошлого хода (для подсказки)
        self._recent_changes: deque = deque(maxlen=10)     # важные изменения (для checkpoint при потере фокуса)
        self._fight               = pa.FightBudget()
        self._last_turn_tick: int | None = None
        self._last_turn_at        = 0.0
        self._speed               = 1                      # скорость игры, которую поставила политика наблюдения
        self._speed_reset_req     = False
        self._quiet_since         = time.time()
        self._last_speed_step     = 0.0
        self._task_first_seen: dict[str, int] = {}
        self._stall_reported: set[str] = set()
        self._forced: dict[str, dict] = {}                 # задача -> {pawn, tick}: принудительная работа
        self._threat_gone_since: float | None = None
        self._paused_by_focus     = False
        self._game_seen_running   = False                  # игра работала при фокусе модуля (сторож поднимет, если упала)
        # правка fix/focus-bootstrap (focus_mode): кто держит фокус и почему модуль вне игры
        self._core_focus_seen     = False                  # ядро присылало policy.activate или focus.release: менеджер фокуса есть
        self._focus_source        = "none"                 # auto (сам), button (кнопка), core (ядро), none
        self._tick_state: dict    = {}                     # состояние игры с последнего тика (постройки для статуса ТЗ 18)
        self._held_off            = False                  # фокус отпущен кнопкой: сам не берём, пока игра не закроется
        self._game_up             = False                  # мод отвечает (игра запущена) по последнему опросу
        self._game_gone_ticks     = 0                      # сколько опросов подряд игра не отвечает

    def run(self):
        asyncio.run(self._run_async())

    # ── Запуск всех контуров ─────────────────────────────────────────────────

    async def _run_async(self):
        logger.info("GLaDOS controller starting…")

        # Игра — необязательный адаптер: ядро работает и без неё, игровые контуры
        # просто простаивают, пока RimWorld не отвечает. Автозапуск игры — по желанию.
        # ТЗ 11e, POL-12: модуль стартует вне фокуса (политика afk) и игру сам НЕ запускает: только по просьбе
        # (инструмент launch_game), при фокусе с настройкой autostart_game или сторожем, если игра упала при фокусе.
        if not _ping():
            logger.info("RimWorld не запущена: вне фокуса игру не запускаю, жду просьбы или фокуса.")
        if self.recorder is not None:
            self._start_watchdog()

        await self._tick_loop()

    def _start_watchdog(self):
        from game_watchdog import GameWatchdog
        self.watchdog = GameWatchdog(
            settings_get=settings.load, recorder=self.recorder, launch=self._launch_rimworld,
            new_colony=self._new_colony, mod_ready=_ping,
            state_ready=lambda: _state_ready(_get_state()), send_camera=_camera_command,
            on_camera_lost=lambda: self.director and self.director.reset(),
            get_state=_get_state, send_command=_game_command,
            module_setting=lambda key, default: (self.link.module_settings if self.link else {}).get(key, default),
            launch_allowed=self._launch_allowed)
        asyncio.create_task(self.watchdog.run())

    # ── ТЗ 11e: запуск игры, фокус, afk ──────────────────────────────────────────────────────

    def _module_setting(self, key: str, default):
        return (self.link.module_settings if self.link else {}).get(key, default)

    def _launch_allowed(self) -> bool:
        """POL-12: сторож и автозапуск поднимают игру только при фокусе модуля (и настройке или после падения)."""
        return pa.launch_allowed(in_focus=self.policies.in_focus,
                                 autostart=bool(self._module_setting("autostart_game", False)),
                                 seen_running_in_focus=self._game_seen_running)

    async def launch_game(self, reason: str) -> str:
        """Инструмент launch_game (явная просьба, POL-12(в)): запуск с объявлением (game_launcher.launch говорит заранее)."""
        if _ping():
            return "игра уже запущена"
        await asyncio.to_thread(self._launch_rimworld, reason)
        return f"запуск игры: {reason}"

    def policy_state(self) -> dict:
        """Политика, правила наблюдения, ручное управление, фокус и скорость для state (дашборд, панель RimWorld)."""
        return {"policy": self.policies.snapshot(), "speed": self._speed,
                "manual": {"forced": sorted(self._forced)[:5], "stalled": sorted(self._stall_reported)[:5]},
                "focus": self._focus_public(), "status": self.focus_status()}

    def focus_status(self) -> dict:
        """Блок `status` для ядра (ТЗ 18, FOC-1, MOD-33): политика, срочность, прерываемость, блокировка, ресурсы."""
        buildings = (self._tick_state.get("colony") or {}).get("buildings") or []
        return focus_status.build(self.policies.policy, goal=self._active_ambition_title(), tasks=self._load_tasks(),
                                  buildings=buildings)

    # ── фокус: режим focus_mode (правка fix/focus-bootstrap) ─────────────────────────────────

    def _focus_mode(self) -> str:
        return focus_mode.normalize(self._module_setting("focus_mode", focus_mode.DEFAULT))

    def _focus_public(self) -> dict:
        mode = self._focus_mode()
        afk = not self.policies.in_focus
        return {"mode": mode, "mode_label": focus_mode.MODE_LABEL[mode], "source": self._focus_source,
                "core_manager": self._core_focus_seen, "held_off": self._held_off, "game_up": self._game_up,
                "why_afk": focus_mode.afk_explanation(mode=mode, core_seen=self._core_focus_seen, game_up=self._game_up,
                                                      held_off=self._held_off) if afk else ""}

    def _focus_watch(self, state: dict) -> None:
        """Каждый опрос. Режим auto без менеджера фокуса в ядре: модуль берёт фокус сам, когда игра запущена. Игра закрыта
        (несколько опросов подряд без ответа): фокус снимается и модуль уходит в afk, в том числе когда среду дало ядро: иначе
        после вылета игры ядро считало бы, что она всё ещё играет (по возврату в фоновую политику оно само выйдет из среды)."""
        if not state:
            self._game_gone_ticks += 1
            if self._game_gone_ticks < focus_mode.GAME_GONE_TICKS:
                return
            self._game_up = False
            self._held_off = False                       # игру закрыли: отпущенный фокус снова доступен после запуска
            if self.policies.in_focus and self._focus_source in ("auto", "button", "core"):
                self._focus_source = "none"
                self._game_seen_running = False          # игру закрыли сами: сторож её не поднимает вне фокуса (POL-12)
                self.policies.enter_afk("игра закрыта: фокус модуля снят, модуль ждёт игру", source="auto")
            return
        self._game_gone_ticks = 0
        if not self._game_up:
            self._emit_game_started()                    # ТЗ 32, WS-5: игра запущена - только сигнал, среду он не включает
        self._game_up = True
        if focus_mode.auto_grab_allowed(mode=self._focus_mode(), core_seen=self._core_focus_seen, game_up=True,
                                        in_focus=self.policies.in_focus, held_off=self._held_off):
            if self.policies.leave_afk("observe", "игра запущена, менеджера фокуса в ядре нет: модуль взял фокус сам",
                                       source="auto"):
                self._focus_source = "auto"

    def _emit_game_started(self) -> None:
        """Сигнал `rimworld.game_started` ядру (ТЗ 32, WS-5). Агент может ответить своим импульсом «зайти в колонию»."""
        link = getattr(self, "link", None)
        if link is None:
            return
        try:
            asyncio.get_running_loop().create_task(link.event("game_started", {}, text="Игра RimWorld запущена."))
        except RuntimeError:
            pass                                         # вне цикла событий (тесты, разовый вызов): сигнал не нужен

    async def _take_focus(self, target: str, reason: str, source: str, cp: dict | None = None) -> dict:
        """Вернуть фокус в `target` и восстановить контур по checkpoint (POL-14). Общая часть policy.activate, focus.resume
        и кнопки. `cp`: checkpoint, который ядро вернуло нам при focus.release; без него берётся файл модуля."""
        if cp is None:
            cp = await asyncio.to_thread(focus_checkpoint.load)
        if not self.policies.leave_afk(target, reason, source=source):
            return {"ok": False, "error": f"нельзя перейти из «{self.policies.current}» в «{target}»"}
        self._focus_source = source
        self._held_off = False
        if cp:
            self._recent_changes.extend(str(c) for c in _as_list(cp.get("changes")))
            self._pending_events.extend(str(p) for p in _as_list(cp.get("pending")))
            if cp.get("paused_by_module"):
                await asyncio.to_thread(_game_command, {"type": "resume"})
            await asyncio.to_thread(focus_checkpoint.clear)
        return {"ok": True, "policy": target, "restored": bool(cp), "goal": (cp or {}).get("goal", "")}

    async def _drop_focus(self, reason: str, source: str) -> dict:
        """Забрать фокус (POL-14): checkpoint, пауза, если игра идёт, уход в afk. Общая часть focus.release и кнопки."""
        if not self.policies.in_focus:
            return {"ok": True, "already": True, "policy": self.policies.current}
        st = await asyncio.to_thread(_get_state)
        paused = False
        if _state_ready(st) and not st.get("paused"):
            paused = bool(await asyncio.to_thread(_game_command, {"type": "pause"}))
        self._paused_by_focus = paused
        self._game_seen_running = False           # фокус уходит: сторож больше не поднимает игру сам (POL-12)
        cp = focus_checkpoint.build(policy=self.policies.current, reason=reason, goal=self._active_ambition_title(),
                                    step=self._step_line(), changes=list(self._recent_changes),
                                    pending=[getattr(e, "text", str(e)) for e in self._pending_events],
                                    paused_by_module=paused)
        saved = await asyncio.to_thread(focus_checkpoint.save, cp)
        self.policies.enter_afk(reason, source=source)
        self._focus_source = "none"
        self._fight.reset()
        self._pending_events.clear()
        # ТЗ 18, MOD-32: ответ focus.release несёт сам checkpoint (ядро хранит его и вернёт в focus.resume)
        return {"ok": True, "checkpoint": cp if saved else None, "paused": paused, "policy": self.policies.current}

    async def activate(self, body: dict) -> dict:
        """Запрос ядра policy.activate (POL-14): вернуть фокус, восстановить контур по checkpoint."""
        self._core_focus_seen = True                 # менеджер фокуса в ядре есть: модуль больше сам фокус не берёт
        policy = str(body.get("policy") or "observe")
        goal = str(body.get("goal") or "")
        if self._focus_mode() == "manual":
            reason = "режим фокуса «вручную»: ядро фокус не получает"
            events_log.log("game", f"запрос ядра policy.activate отклонён: {reason}")
            return {"ok": False, "error": reason + "; фокус даёт кнопка «Взять фокус» на панели RimWorld"}
        if self.policies.in_focus:
            self._focus_source = "core"
            return {"ok": True, "already": True, "policy": self.policies.current}
        target = "observe" if policy == "afk" else policy
        return await self._take_focus(target, f"фокус возвращён: {goal[:100]}".strip(), source="core")

    async def resume(self, body: dict) -> dict:
        """Запрос ядра focus.resume (ТЗ 18, FOC-4): возобновить задачу по checkpoint ядра (или по файлу модуля). Подцели
        попадают в подсказку хода. Режим «вручную» отказывает, как policy.activate."""
        self._core_focus_seen = True                 # менеджер фокуса в ядре есть
        if self._focus_mode() == "manual":
            reason = "режим фокуса «вручную»: ядро фокус не получает"
            events_log.log("game", f"запрос ядра focus.resume отклонён: {reason}")
            return {"ok": False, "error": reason + "; фокус даёт кнопка «Взять фокус» на панели RimWorld"}
        if self.policies.in_focus:
            self._focus_source = "core"
            return {"ok": True, "already": True, "policy": self.policies.current}
        policy = str(body.get("policy") or "observe")
        target = "observe" if policy == "afk" else policy
        goal = str(body.get("goal") or "")
        cp = body.get("checkpoint") if isinstance(body.get("checkpoint"), dict) else None
        subgoals = [f"подцель: {s}"[:200] for s in _as_list(body.get("subgoals")) if isinstance(s, str)][:5]
        res = await self._take_focus(target, f"задача возобновлена: {goal[:100]}".strip(), source="core", cp=cp)
        if res.get("ok") and subgoals:
            self._recent_changes.extend(subgoals)
        return res

    async def release(self, body: dict) -> dict:
        """Запрос ядра focus.release (POL-14): сохранить checkpoint, поставить игру на паузу, если она идёт, уйти в afk."""
        self._core_focus_seen = True                 # менеджер фокуса в ядре есть
        reason = str(body.get("reason") or "фокус у другой задачи")
        return await self._drop_focus(reason, source="core")

    async def panel_take_focus(self, args: dict) -> dict:
        """Кнопка «Взять фокус» на панели RimWorld: то же, что policy.activate, без запроса ядра."""
        if self.policies.in_focus:
            return {"ok": True, "message": "фокус уже у модуля"}
        if not await asyncio.to_thread(_ping):
            return {"ok": False, "message": "игра не запущена: фокус берётся, когда она открыта (модуль её сам не запускает)"}
        res = await self._take_focus("observe", "фокус взят кнопкой на панели", source="button")
        if not res["ok"]:
            return {"ok": False, "message": res["error"]}
        return {"ok": True, "message": "фокус взят: модуль ведёт игру"}

    async def panel_release_focus(self, args: dict) -> dict:
        """Кнопка «Отпустить фокус»: checkpoint и пауза, как у focus.release. Сам фокус до закрытия игры не берётся."""
        res = await self._drop_focus("фокус отпущен кнопкой на панели", source="button")
        if res.get("already"):
            return {"ok": True, "message": "фокуса и так нет"}
        self._held_off = True
        return {"ok": True, "message": "фокус отпущен" + (": игра на паузе" if res.get("paused") else "")
                + "; модуль сам его не возьмёт, пока игра не закроется или не нажата «Взять фокус»"}


    async def _tick_afk(self, state: dict) -> None:
        """Вне фокуса (afk): только дешёвые сигналы. Критические события игры уходят ядру с признаком «нужен фокус»."""
        self._prev_snap = None
        self._tick_now = state.get("tick")
        events = await asyncio.to_thread(_get_events)
        for evt in events:
            ev = policy_events.from_mod(evt)
            if ev is None or ev.importance < 4 or self.link is None:
                continue
            kind = {"colony_died": "run_ended"}.get(ev.kind, ev.kind)
            asyncio.create_task(self.link.event(kind, {**ev.fields, "needs_focus": True}, text=ev.text,
                                                urgency=ev.importance))

    def _step_line(self) -> str:
        tasks = self._load_tasks()
        current = next((t for t in tasks if t.get("status") == "in_progress"), None) or \
            next((t for t in tasks if t.get("status") == "pending"), None)
        return str(current.get("description")) if current else ""

    def _load_tasks(self) -> list[dict]:
        return []                          # задачи ведёт единый агент ядра (assistant.tasks); файлы прежнего игрового агента не читаются

    def _active_ambition_title(self) -> str:
        return ""

    def _exit_quiet_s(self, policy_id: str) -> float:
        return self.policies.reg.policies[policy_id].exit_quiet_s

    async def _new_colony(self):
        """Создать поселение из главного меню. Сначала быстро и точно: нажать кнопку «Быстрый запуск (Dev)» (до трёх попыток:
        окно может ещё не отрисоваться, а после лишнего клика экран другой), потом нажатие по тексту кнопки через OCR, и только
        затем навигация через Claude по скриншотам (она долгая и часто упирается в таймаут)."""
        if self._navigating:
            return
        import game_watchdog
        self._navigating = True
        try:
            for attempt in range(1, QUICK_START_ATTEMPTS + 1):
                pressed = await asyncio.to_thread(game_watchdog.press_quick_start)
                if pressed:
                    import game_launcher
                    game_launcher.announce_self("нажимаю «Быстрый запуск» в главном меню: поселения нет, создаю новое, "
                                                "чтобы игра шла", kind="game")       # правило 14: заметное действие в игре
                    logger.info(f"Нажала «Быстрый запуск» (попытка {attempt}): жду загрузку поселения")
                    for _ in range(QUICK_START_WAIT_POLLS):
                        await asyncio.sleep(2)
                        if _state_ready(await asyncio.to_thread(_get_state)):
                            return
                    logger.info("Поселение не загрузилось после «Быстрого запуска»: пробую ещё раз")
                else:
                    logger.info(f"Кнопка «Быстрый запуск» не найдена на месте (попытка {attempt}): жду и пробую снова")
                    await asyncio.sleep(3)
        finally:
            self._navigating = False
        await self._menu_autostart()                     # кнопка по тексту (OCR), без Claude
        for _ in range(QUICK_START_WAIT_POLLS):
            await asyncio.sleep(2)
            if _state_ready(await asyncio.to_thread(_get_state)):
                return

    async def _sync_camera_switch(self) -> None:
        """Переключатель «камерой управляет агент». Главный выключатель живёт в моде (кнопка «CAM» у края экрана игры, команда
        `set_agent_camera`; при каждом запуске игры он включён). Модуль только передаёт моду настройку `camera_agent`, когда её
        изменили в дашборде или когда игра запустилась с выключенной настройкой. Кнопку в игре настройка не перебивает."""
        want = bool(self._module_setting("camera_agent", True))
        game_up = bool(self._game_up)
        if getattr(self, "_camera_pushed", None) == (want, game_up):
            return
        was = getattr(self, "_camera_pushed", None)
        self._camera_pushed = (want, game_up)
        if not game_up or (was is None and want):
            return                                       # игры нет, или первый опрос с включённой настройкой (в моде и так включено)
        logger.info("Камера: " + ("управляет агент" if want else "у игрока, агент её не двигает"))
        await asyncio.to_thread(_camera_command, {"type": "set_agent_camera", "enabled": want})

    async def _camera(self, cmds: list[dict]):
        """Отправить команды камеры в мод (не блокируя тик). Сбой камеры не должен влиять на игру."""
        for cmd in cmds:
            await asyncio.to_thread(_camera_command, cmd)

    def _launch_rimworld(self, reason: str = "наблюдатель за игрой: игра не запущена"):
        try:
            import game_launcher
            game_launcher.launch(reason, wait_seconds=0, args=game_launcher.windowed_args())      # всегда окно: полноэкранная игра сворачивается и становится слепой для агента
            logger.info("RimWorld запускается")
        except Exception as e:
            logger.error(f"Не удалось запустить RimWorld: {e}")

    # ── Контур 1: реагирование (быстрый) ─────────────────────────────────────

    async def _tick_loop(self):
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except KeyboardInterrupt:
                logger.info("Завершение.")
                break
            except Exception as e:
                logger.error(f"Ошибка тика: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_SECS)

    async def _tick(self):
        state = await asyncio.to_thread(_get_state)
        self._tick_state = state or {}
        self._focus_watch(state)
        if not _state_ready(state):
            if state.get("status") == "no_game":
                if not self._menu_greeted:
                    self._menu_greeted = True
                    self._menu_since = time.time()
                elif (self.watchdog is None and not self._navigating and self._menu_since and self.policies.in_focus and
                      time.time() - self._menu_since > MENU_STUCK_THRESHOLD_SECS):   # с наблюдателем меню проходит он; вне фокуса не трогаем игру (POL-11)
                    self._menu_since = time.time()
                    asyncio.create_task(self._menu_autostart())          # кнопка по тексту (OCR), без Claude
                    return
            return

        self._menu_greeted = False  # снова в игре — при возврате в меню поздоровается заново
        self._menu_since   = None
        if not self.policies.in_focus:          # ТЗ 11e POL-11: вне фокуса игровой ход не ведём
            await self._tick_afk(state)
            return

        current_tick = state.get("tick", 0)
        self._tick_now = current_tick
        self._game_seen_running = True
        self._seen_game_errors, fresh_errors = game_errors.new_errors(state.get("game_log"), self._seen_game_errors)
        for line in fresh_errors:
            logger.warning(f"Ошибка игры: {line}")
            events_log.log("game", f"ошибка игры (журнал отладки закрыт, ошибка записана): {line}")
        self._ensure_run_started(state)
        snap = statediff.snapshot(state)
        if self._prev_snap is not None:
            for important, line in statediff.compare(self._prev_snap, snap):
                if important:
                    events_log.log("game", "изменение: " + line)
                    self._recent_changes.append(line)
        self._prev_snap = snap
        self._rel.sync_colonists((state.get("colony") or {}).get("pawns") or [])
        if self.link is not None and time.time() - self._graph_sent >= 60:
            self._graph_sent = time.time()
            pawns = (state.get("colony") or {}).get("pawns") or []
            colony_name = (state.get("colony") or {}).get("name") or "колония"
            asyncio.create_task(self.link.event("colonists", {"pawns": pawns, "colony": colony_name},
                                                text=f"Колония «{colony_name}»: колонистов {len(pawns)}."))
        await self._sync_camera_switch()
        events = await asyncio.to_thread(_get_events)
        filming = self.director is not None and bool(settings.load().get("record_runs"))
        if filming:
            await self._camera(self.director.update(state))

        for evt in events:
            events_log.log("game", event_text.describe(evt), type=evt.get("type"),
                           priority=evt.get("priority"))
            if self.link is not None:
                await self._send_game_event(evt)
            if filming:
                await self._camera(self.director.on_event(evt, state))
            if self.recorder is not None and evt.get("type") in ("pawn_died", "incident"):
                d = evt.get("data", {})
                self.recorder.mark(f"погиб колонист {d.get('name', '?')}" if evt["type"] == "pawn_died"
                                   else f"налёт или угроза: {d.get('label')}", 3 if evt["type"] == "pawn_died" else 2)
            if evt.get("type") == "pawn_died":
                d = evt.get("data", {})
                self._rel.mark_dead(d.get("name", "?"), d.get("cause", ""))
                self._memory.record_pawn_fate(
                    d.get("pawn_id", "?"), d.get("name", "?"),
                    fate="dead", cause=d.get("cause", "unknown"),
                )
            if evt.get("type") == "game_loaded":
                d = evt.get("data", {})
                logger.info(f"Колония загружена: {d.get('colony')} (тик {d.get('tick')})")
                if self.on_phase_change is not None:
                    self.on_phase_change()
                self._last_proactive_tick = 0
                self._colony_started      = False
                self._just_loaded         = True
                self._prev_snap = self._turn_snap = None
                if self.director is not None:
                    self.director.reset()
            if evt.get("type") == "colony_died":
                d = evt.get("data", {})
                logger.info(f"Колония погибла: {d.get('colony')} (тик {d.get('tick')})")
                self._end_run("colony_died", f"Все колонисты погибли на тике {d.get('tick')}", importance=3)
                if self.link is not None:
                    asyncio.create_task(self.link.event("run_ended", {
                        "outcome": "colony_died", "tick": d.get("tick"), "colony": d.get("colony"),
                        "prompt": (f"Забег закончен: вся колония погибла (тик {d.get('tick')}). Напиши короткий пост-итог "
                                   "об этом забеге: что пошло не так и какой вывод ты делаешь.")},
                        text=f"Забег закончен: вся колония «{d.get('colony') or 'без имени'}» погибла (тик {d.get('tick')})."))
                if self.on_phase_change is not None:
                    self.on_phase_change()
                self._colony_started = False                # новую колонию начнёт агент (среда rimworld) или пользователь

        self._had_threats = bool(state.get("threats"))

    # ── игровой ход: фоновая задача, которую можно прервать ───────────────────

    # письма, о которых ядро уже знает отдельным событием (набег, гибель колониста): второй раз их не шлём
    _COVERED_LETTERS = ("ThreatBig", "ThreatSmall", "Death")
    _LETTER_URGENCY = {"critical": 4, "high": 3}

    async def _send_game_event(self, evt: dict):
        """Событие игры -> событие модуля для ядра (RW-9): набег, гибель колониста, пожар, письмо. Ядро решает, важно ли оно
        сейчас; комментатор говорит по этим же событиям сам. Текст это факт, а не реплика."""
        link, kind, d = self.link, evt.get("type"), evt.get("data") or {}
        if link is None:
            return
        if kind == "incident":
            label = d.get("label") or d.get("incident") or "угроза"
            event = link.event("raid", {"incident": d.get("incident"), "label": d.get("label"), "points": d.get("points")},
                               text=f"Угроза колонии: {label}.", urgency=5 if evt.get("priority") == "critical" else 4)
        elif kind == "pawn_died":
            event = link.event("death", {"name": d.get("name"), "cause": d.get("cause"), "age": d.get("age")},
                               text=event_text.describe(evt))
        elif kind == "fire_started":
            event = link.event("fire", {"x": d.get("x"), "z": d.get("z")}, text=event_text.describe(evt))
        elif kind == "letter" and d.get("letter_def") not in self._COVERED_LETTERS:
            event = link.event("letter", {"label": d.get("label"), "letter_def": d.get("letter_def")},
                               text=event_text.describe(evt), urgency=self._LETTER_URGENCY.get(evt.get("priority"), 2))
        else:
            return
        asyncio.create_task(event)

    async def _check_goals(self):
        """Цель игры закрыта (достигнута или заброшена) -> событие rimworld.goal. Статусы пишет MCP-сервер в memory/goals.json
        (отдельный процесс Claude), поэтому файл сверяется по времени изменения."""
        changes = await asyncio.to_thread(self._goal_changes)
        for goal in changes:
            done = goal.get("status") == "completed"
            title = goal.get("title") or goal.get("id")
            asyncio.create_task(self.link.event("goal", {
                "goal_id": goal.get("id"), "title": goal.get("title"), "status": goal.get("status"),
                "category": goal.get("category"), "progress_pct": goal.get("progress_pct")},
                text=f"Цель игры «{title}» " + ("достигнута." if done else "заброшена.")))

    def _goal_changes(self) -> list[dict]:
        import json
        mem = os.getenv("MEMORY_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
        path = os.path.join(mem, "goals.json")
        try:
            mtime = os.path.getmtime(path)
            if mtime == self._goal_mtime:
                return []
            with open(path, encoding="utf-8") as f:
                goals = json.load(f)
        except (OSError, ValueError):
            return []
        self._goal_mtime = mtime
        known = self._goal_status
        self._goal_status = {g.get("id"): g.get("status") for g in goals if isinstance(g, dict)}
        if known is None:
            return []                       # первая проверка только запоминает, что было
        return [g for g in goals if isinstance(g, dict) and known.get(g.get("id")) == "active"
                and g.get("status") in ("completed", "abandoned")]

    def _end_run(self, outcome: str, notes: str, importance: int = 2):
        """Закрыть забег в памяти и отдать запись на сценарий, монтаж и клипы (в фоне, ход игры не ждёт)."""
        self._memory.end_run(outcome, notes)
        if self.recorder is None:
            return
        self.recorder.mark(notes, importance)
        rec = self.recorder.close(outcome)       # цели фиксируются сразу: новый забег убирает их в архив
        if rec is not None:
            asyncio.create_task(self._produce_video(rec))

    async def _produce_video(self, rec):
        # ТЗ 20 (MON-35, MON-36): при настройке `auto_montage` и подключённом модуле montage монтаж забега отдаётся ему; своего
        # монтажа и клипов тогда нет. Модуль не ответил (не подключён, нет заряда, отказ) - прежний собственный монтаж.
        try:
            if self._auto_montage_on():
                handoff = await self.recorder.produce_for_montage(rec)
                if handoff is None:
                    return                                             # запись пустая или короткая
                started = await self.link.start_montage(handoff)
                if started:
                    await self.link.event("run_video", {
                        "run_id": handoff["run_id"], "raw": handoff["raw"], "scenario": handoff["scenario_path"],
                        "montage_jobs": started}, text="Запись забега передана модулю монтажа.")
                    return
                events_log.log("video", "модуль монтажа не принял забег: монтирую сама")
                result = await asyncio.to_thread(self.recorder.process, rec)
            else:
                result = await self.recorder.produce(rec)
        except Exception as e:
            logger.error(f"Видео забега: {e}", exc_info=True)
            return
        if result and self.link is not None:
            await self.link.event("run_video", {
                "run_id": result["run_id"], "video": result["video"], "raw": result.get("raw"),
                "scenario": result.get("scenario_path"), "clips": result["clips"]},
                text=("Видео забега готово." if result["video"] else "Запись забега есть, но монтаж не получился."))

    def _auto_montage_on(self) -> bool:
        link = self.link
        return bool(link is not None and link.online and (link.module_settings or {}).get("auto_montage"))

    def _ensure_run_started(self, state: dict):
        if self._colony_started:
            return

        colony_name = state.get("colony", {}).get("name", "Unknown Colony")
        save_id     = state.get("save_id", "")
        run         = self._memory.get_current_run()
        knowledge.set_save(save_id)

        if run and save_id and run.get("save_id") != save_id:
            logger.info(f"Обнаружена другая колония/сейв (было: {run.get('colony_name')}, "
                        f"стало: {colony_name}) — завершаю старый эксперимент.")
            self._end_run("session_changed", "Загружен другой сейв")
            run = None

        if not run:
            run = self._memory.start_run(colony_name, save_id)
            logger.info(f"Начат эксперимент: {colony_name} (save_id={save_id})")
            if self.recorder is not None:
                self.recorder.start(run["id"], colony_name)
        else:
            if self.recorder is not None:
                self.recorder.start(run["id"], colony_name,     # бот перезапускался посреди забега
                                    why="бот перезапускался посреди забега: продолжаю запись того же забега")
            # Процесс мог перезапуститься посреди забега — восстанавливаем
            # последнюю реплику, чтобы anti-repeat защита не забывала её
            decisions = run.get("key_decisions", [])
            if decisions:
                last_directive = decisions[-1].get("directive", "")
                if last_directive and last_directive != "(молчание)":
                    self._last_commentary = last_directive

        self._colony_started = True

    # ── Контур 2: тактическое планирование (средний темп) ────────────────────

    # ── Контур 3: долгосрочное планирование (медленный темп) ─────────────────

    # ── Контур 4: рефлексия (самый медленный темп) ────────────────────────────

    # ── Контур 5: самоулучшение (самый редкий, самый мощный) ─────────────────
    # Полный доступ к файлам проекта (Read/Write/Edit/Bash), правит свой же
    # код и код мода. Правки controller.py/glados_agent.py применятся только
    # после перезапуска процесса main.py — сам себя контур не перезапускает.

    # ── Контур 6: навигация по UI (разовые вызовы, не периодический цикл) ────

    async def _menu_autostart(self):
        """Игра стоит в главном меню, а Claude недоступен: нажать кнопки меню без него (OCR по окну игры через ассистентку).
        Сначала «Быстрый запуск» (режим разработчика), иначе «Продолжить»."""
        if self._navigating:
            return
        self._navigating = True
        try:
            for label in ("Быстрый запуск", "Quick Start", "Продолжить", "Continue"):
                try:
                    r = await asyncio.to_thread(
                        lambda lb=label: httpx.post(f"{_ASSISTANT_URL}/api/menu_click", json={"text": lb}, timeout=60).json())
                except Exception as e:
                    logger.info(f"Автозапуск: ассистентка недоступна ({e})")
                    return
                if r.get("ok"):
                    logger.info(f"Автозапуск: нажала «{label}» ({r.get('result')})")
                    return
            logger.info("Автозапуск: подходящей кнопки меню на экране не нашла")
        finally:
            self._navigating = False
