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
import time
from pathlib import Path

import httpx
import claude_quota
from collections import deque

from glados_agent import (run_turn, run_planning, run_long_term_planning,
                          run_reflection, run_self_improvement, run_game_navigation)
from memory import Memory
import agent_mode
import events_log
import fast_brain
import game_errors
import gamenet
import planning_pause
import claude_budget
import settings
import statediff
from relationships import Relationships
import decider
import focus_checkpoint
import focus_mode
import focus_status
import policy_actions as pa
import policy_context
import policy_events
import policy_manager
import watch_rules
from glados_agent import run_plan_check

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
_ASSISTANT_URL = os.getenv("GLADOS_ASSISTANT_URL", "http://127.0.0.1:8766")
MENU_STUCK_THRESHOLD_SECS = 90              # торчим в главном меню дольше — проходим сами через UI
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


def _is_game_talk(text: str, pawn_names: list[str]) -> bool:
    """Сообщение игрока про игру (приказ или тревога), а не просто болтовня."""
    low = text.lower()
    if any(w in low for w in _GAME_TALK):
        return True
    return any(n and len(n) > 2 and n.lower() in low for n in pawn_names)


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
        return httpx.get(f"{_BASE}/state", timeout=5).json()
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


def _similarity(a: str, b: str) -> float:
    """Доля общих слов (Jaccard) — грубая, но дешёвая проверка на повтор."""
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


MAX_COMMENTARY_CHARS  = 220   # жёсткий потолок длины — подстраховка поверх промпта
REPEAT_SIMILARITY_MAX = 0.55  # выше — считаем повтором предыдущей реплики


def _trim_commentary(text: str) -> str:
    if len(text) <= MAX_COMMENTARY_CHARS:
        return text
    cut = text[:MAX_COMMENTARY_CHARS]
    last_stop = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return cut[:last_stop + 1] if last_stop > MAX_COMMENTARY_CHARS // 2 else cut.rstrip() + "…"


class Controller:
    def __init__(self, config: dict):
        self._memory = Memory(config.get("memory_dir", "./memory"))
        fast_brain.init(config.get("memory_dir", "./memory"))
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
        self.commentator          = None   # Commentator: живые реплики по ходу игры (локальная модель)
        self._graph_sent          = 0.0    # когда состав колонии в последний раз ушёл ассистенту (для графа памяти)
        self._prev_snap           = None   # снимок прошлого цикла: из разницы важное идёт в протокол событий
        self._turn_snap           = None   # снимок на начало её прошлого хода: из разницы «что изменилось»
        self._had_threats         = False  # были враги на карте: когда исчезнут, наступает «бой окончен»
        self._turn_kind           = ""
        self._turn_started        = 0.0
        # ТЗ 11e: политики, правила наблюдения, бюджеты боя, ручное управление, скорость, checkpoint фокуса
        self.policies             = policy_manager.PolicyManager(log=_policy_log)
        self.watch                = watch_rules.WatchRules()
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

    def add_chat_message(self, message: str):
        self._chat_buffer.append(message)
        # Рефлекс: сразу оценить тон сообщения (эмоция + отношение к игроку)
        try:
            asyncio.get_running_loop().create_task(fast_brain.react_chat(message, self._rel))
        except RuntimeError:
            pass  # вызвано вне event loop — пропускаем рефлекс, Claude всё равно увидит сообщение

    def _context(self) -> str:
        """Контекст для Claude: память архива + личные отношения."""
        import action_journal
        import knowledge
        situation = self._ambition_block() + self._tasks_block() + (self._turn_reason or "")
        import colony_intel
        st = getattr(self, "_last_state", None) or {}
        intel = colony_intel.age_block(st) + colony_intel.survey_block(st, colony_intel.load_survey(st.get("save_id", "")))
        return (intel + self._ambition_block() + self._tasks_block() + action_journal.block() + knowledge.block(situation)
                + self._memory.build_context_block() + self._rel.summary())

    @staticmethod
    def _tasks_block() -> str:
        """Активные задачи деревом: ход должен их ВЫПОЛНЯТЬ (разметка, приказы), а не только смотреть на колонию."""
        import tasks_tree
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "tasks.json"), encoding="utf-8") as f:
                import json
                tree = tasks_tree.build(json.load(f))
        except (OSError, ValueError):
            return ""
        lines = []

        def walk(nodes, depth):
            for n in nodes:
                if n["status"] == "done":
                    continue
                lines.append("  " * depth + f"- [{n['status']}] {n['text'][:140]}")
                walk(n["children"], depth + 1)
        walk(tree, 0)
        if not lines:
            return ""
        return ("=== ТВОИ ЗАДАЧИ (план). Бери верхние невыполненные и ДЕЛАЙ их прямо в этом ходе: разметка, заказы, "
                "приказы; готовому шагу ставь set_task(..., status=\"done\") ===\n"
                + "\n".join(lines[:16]) + "\n\n")

    @staticmethod
    def _ambition_block() -> str:
        """Главная цель игры (цель категории ambition) всегда на виду: ход начинается с неё."""
        goal = _active_ambition()
        if goal is None:
            return ("=== ГЛАВНОЙ ЦЕЛИ ИГРЫ ЕЩЁ НЕТ ===\nПоставь её сейчас: set_goal(category=\"ambition\", "
                    "priority=\"high\"), измеримую и амбициозную, на всю игру.\n\n")
        return (f"=== ГЛАВНАЯ ЦЕЛЬ ИГРЫ ===\n{goal.get('title')} (прогресс {goal.get('progress_pct', 0)}%)\n"
                f"Критерий: {goal.get('success_criteria')}\nЗаметки: {goal.get('notes', '')}\n"
                "Каждый твой ход должен приближать колонию к ней.\n\n")

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
        asyncio.create_task(fast_brain.warmup())
        asyncio.create_task(self._planning_loop())
        asyncio.create_task(self._long_term_planning_loop())
        asyncio.create_task(self._reflection_loop())
        asyncio.create_task(self._self_improvement_loop())

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
        return {"policy": self.policies.snapshot(), "watch_rules": self.watch.public(self._tick_now),
                "watch_fired": self.watch.fired_public(), "speed": self._speed,
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
        (несколько опросов подряд без ответа): свой фокус снимает и уходит в afk. Фокус от ядра здесь не трогаем."""
        if not state:
            self._game_gone_ticks += 1
            if self._game_gone_ticks < focus_mode.GAME_GONE_TICKS:
                return
            self._game_up = False
            self._held_off = False                       # игру закрыли: отпущенный фокус снова доступен после запуска
            if self.policies.in_focus and self._focus_source in ("auto", "button"):
                self._focus_source = "none"
                self._game_seen_running = False          # игру закрыли сами: сторож её не поднимает вне фокуса (POL-12)
                self.policies.enter_afk("игра закрыта: фокус модуля снят, модуль ждёт игру", source="auto")
            return
        self._game_gone_ticks = 0
        self._game_up = True
        if focus_mode.auto_grab_allowed(mode=self._focus_mode(), core_seen=self._core_focus_seen, game_up=True,
                                        in_focus=self.policies.in_focus, held_off=self._held_off):
            if self.policies.leave_afk("observe", "игра запущена, менеджера фокуса в ядре нет: модуль взял фокус сам",
                                       source="auto"):
                self._focus_source = "auto"

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
        if self._turn_running():
            self._turn_task.cancel()
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

    def _policy_event(self, ev) -> None:
        """Событие для политик: вид с классификатором уточняется в фоне, остальное применяется сразу."""
        if ev is None:
            return
        kind = self.policies.reg.kind(ev.kind)
        if kind is not None and kind.classify:
            asyncio.get_running_loop().create_task(self._classify_and_apply(ev))
        else:
            self._apply_policy_event(ev)

    async def _classify_and_apply(self, ev) -> None:
        ev = await policy_events.classify(ev, decide=lambda text, q: decider.decide(text, q, timeout=1.5))
        self._apply_policy_event(ev)

    def _apply_policy_event(self, ev) -> None:
        """Событие -> правила наблюдения (усиление важности, переход) -> политика по `enter`. Всё пишется в журнал."""
        before = self.policies.current
        self._pending_events.append(ev)
        if ev.kind == "research_done":
            self._research_done(str(ev.fields.get("research") or ""))
        self.watch.refresh()
        rules = self.watch.match(ev.kind, ev.fields)
        imp = ev.importance
        for rule in rules:
            imp = min(5, imp + rule.boost)
        self.policies.on_event(ev.kind, imp, reason=ev.text)
        for rule in rules:
            if rule.switch_to:
                self.policies.rule_switch(rule.switch_to, f"правило {rule.id}: {rule.why}")
            events_log.log("game", f"правило наблюдения сработало: {rule.why} ({ev.text[:80]})")
            self.watch.consume(rule, ev.text, self._tick_now)
        if ev.importance >= 3:
            self._quiet_since = time.time()
            self._speed_reset_req = True
        if self.policies.current == "combat" and before != "combat":
            self._fight.reset()
        if self.policies.current == "plan" and before != "plan":
            self._planning_wake.set()

    def _research_done(self, research: str) -> None:
        """Исследование завершено (ТЗ 11e, исследования): шаги дерева задач, которые его ждали, снова в работе, исследование
        снимается с очереди, ход начинается с возвратом к шагу. Правило наблюдения переводит политику (см. _apply_policy_event)."""
        import research_unlock as ru
        if not research:
            return
        resumed = ru.resume_tasks_file(MEM_DIR / "tasks.json", research)
        ru.pop_done(research)
        if resumed:
            events_log.log("game", f"исследование {research} завершено: возвращаюсь к шагу: {'; '.join(resumed)[:160]}")
            self._need_turn = True

    def _wake_planning_policy(self, reason: str) -> None:
        self._policy_event(policy_events.make("plan_request", reason, {"reason": reason}))

    def _step_line(self) -> str:
        tasks = self._load_tasks()
        current = next((t for t in tasks if t.get("status") == "in_progress"), None) or \
            next((t for t in tasks if t.get("status") == "pending"), None)
        return str(current.get("description")) if current else ""

    def _load_tasks(self) -> list[dict]:
        import json
        try:
            with open(MEM_DIR / "tasks.json", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _turn_brief(self, changes: list[str] | None) -> str:
        """Подсказка хода без полного снимка мира (ТЗ 11e, POL-1): события, важное, цель, шаг, инструменты-запросы."""
        events = list(self._pending_events)
        self._pending_events.clear()
        ambition = self._active_ambition_title()
        import research_unlock as ru
        return policy_context.turn_brief(events, changes or [], policy_title=self.policies.policy.title,
                                         ambition=ambition, step=self._step_line(), research_next=ru.next_research())

    def _active_ambition_title(self) -> str:
        goal = _active_ambition()
        return str(goal.get("title")) if goal else ""

    def _exit_quiet_s(self, policy_id: str) -> float:
        return self.policies.reg.policies[policy_id].exit_quiet_s

    async def _manual_watch(self, snap: dict, tick: int | None, stalled_hours_default: float = 2.0) -> None:
        """POL-…/раздел 3.4: застрявшие задачи -> событие ручного управления; в режиме ручного: принуждение и откат."""
        pol = self.policies.reg.policies.get("manual")
        if pol is None:
            return
        budget = pol.budget
        stall_hours = float(self._module_setting("manual_stall_hours", budget.get("stall_hours", stalled_hours_default)))
        confirm = bool(self._module_setting("manual_confirm", budget.get("confirm", False)))
        tasks = self._load_tasks()
        stalled = pa.stalls(tasks, self._task_first_seen, tick, stall_hours)
        stalled_now = {str(t.get("description")) for t in stalled}
        for t in stalled:
            desc = str(t.get("description"))
            if desc not in self._stall_reported:
                self._stall_reported.add(desc)
                self._policy_event(policy_events.make("task_stalled", f"задача застряла: {desc}", {"description": desc}))
        self._stall_reported &= stalled_now
        if self.policies.current != "manual":
            return
        idle_ids = [pid for pid, p in snap["pawns"].items()
                    if p["job"] in statediff.IDLE_JOBS and not p["drafted"] and not p["downed"] and not p["prisoner"]]
        done = {str(t.get("description")) for t in tasks if t.get("status") == "done"}
        actions = pa.plan_forced(stalled, forced=self._forced, idle_pawn_ids=idle_ids, tick=tick,
                                 interval_hours=float(budget.get("interval_hours", 2)), confirm=confirm)
        actions += pa.plan_rollback(self._forced, done, tick, float(budget.get("rollback_hours", 1)))
        for a in actions:
            await self._manual_action(a, tick)
        if not self._forced:
            self.policies.finish("ручные задачи обработаны")

    async def _manual_action(self, a: dict, tick: int | None) -> None:
        if a["type"] == "force":
            ok = await asyncio.to_thread(_game_command, a["cmd"])
            if ok:
                self._forced[a["task"]] = {"pawn": a["pawn"], "tick": tick if tick is not None else 0}
            events_log.log("game", f"принудительная работа {a['pawn']} → «{a['task'][:80]}»: {'принято' if ok else 'не принято игрой'}")
        elif a["type"] == "rollback":
            await asyncio.to_thread(_game_command, a["cmd"])
            self._forced.pop(a["task"], None)
            events_log.log("game", f"откат принуждения {a['pawn']}: «{a['task'][:80]}» не сдвинулась, очередь сброшена")
        elif a["type"] == "forget":
            self._forced.pop(a["task"], None)
        elif a["type"] == "note":
            events_log.log("game", f"ручное управление: {a['text']} («{a['task'][:80]}»)")

    async def _apply_speed(self, threats_now: bool, now: float) -> None:
        """POL-…/раздел 3.1: наблюдение растёт ступенями скорости в тишине; любое важное событие и не-наблюдение возвращают 1."""
        if self.policies.current != "observe":
            target = 1
        elif self._speed_reset_req:
            target = 1
            self._speed_reset_req = False
        else:
            if threats_now:
                self._quiet_since = now
            pol = self.policies.reg.policies["observe"]
            new = pa.observe_speed_plan(now=now, quiet_since=self._quiet_since, last_step_at=self._last_speed_step,
                                        current_speed=self._speed, ladder=list(pol.time.get("speed_ladder", [1])),
                                        max_speed=int(self._module_setting("observe_max_speed", 3)),
                                        step_quiet_s=float(pol.time.get("step_quiet_s", 120)))
            if new is None:
                return
            target = new
        if target == self._speed:
            return
        if await asyncio.to_thread(_game_command, {"type": "set_time_speed", "speed": target}):
            self._speed = target
            self._last_speed_step = now

    async def _new_colony(self):
        """Создать поселение из главного меню. Сначала быстро и точно: нажать кнопку «Быстрый запуск (Dev)». Если кнопки
        нет на месте или игра не загрузилась, дальше идёт навигация через Claude по скриншотам."""
        if self._navigating:
            return
        import game_watchdog
        self._navigating = True
        try:
            pressed = await asyncio.to_thread(game_watchdog.press_quick_start)
            if pressed:
                import game_launcher
                game_launcher.announce_self("нажимаю «Быстрый запуск» в главном меню: поселения нет, создаю новое, "
                                            "чтобы игра шла", kind="game")       # правило 14: заметное действие в игре
                logger.info("Нажала «Быстрый запуск»: жду загрузку поселения")
                for _ in range(45):
                    await asyncio.sleep(2)
                    if _state_ready(await asyncio.to_thread(_get_state)):
                        return
        finally:
            self._navigating = False
        await self._navigate_to_game(MENU_GOAL)

    async def _camera(self, cmds: list[dict]):
        """Отправить команды камеры в мод (не блокируя тик). Сбой камеры не должен влиять на игру."""
        for cmd in cmds:
            await asyncio.to_thread(_camera_command, cmd)

    def _launch_rimworld(self, reason: str = "наблюдатель за игрой: игра не запущена"):
        try:
            import game_launcher
            game_launcher.launch(reason, wait_seconds=0, args=game_launcher.windowed_args() if settings.load().get("record_runs") else ())
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
                    if True:     # локальный путь (OCR) всегда первым; Claude только если кнопка не нашлась
                        self._menu_since = time.time()
                        asyncio.create_task(self._menu_autostart())
                        return
                    asyncio.create_task(self._navigate_to_game(MENU_GOAL))
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
            own = self._turn_running() or time.time() - self._last_turn_end < 15
            for important, line in statediff.compare(self._prev_snap, snap):
                if own and statediff.is_pause_change(line):
                    continue            # паузу ставит и снимает сама она во время хода: это не событие со стороны
                if important:
                    events_log.log("game", "изменение: " + line)
                    self._recent_changes.append(line)
                    self._policy_event(policy_events.from_statediff(line, True))
                if self.commentator is not None:
                    self.commentator.feed(line, important)
        self._prev_snap = snap
        if self.commentator is not None:
            self.commentator.update_state(
                (state.get("colony") or {}).get("name") or "колония",
                "; ".join(f"{p['name']}: {p.get('job') or '?'}" for p in snap["pawns"].values()))
        self._rel.sync_colonists((state.get("colony") or {}).get("pawns") or [])
        if self.link is not None and time.time() - self._graph_sent >= 60:
            self._graph_sent = time.time()
            pawns = (state.get("colony") or {}).get("pawns") or []
            colony_name = (state.get("colony") or {}).get("name") or "колония"
            asyncio.create_task(self.link.event("colonists", {"pawns": pawns, "colony": colony_name},
                                                text=f"Колония «{colony_name}»: колонистов {len(pawns)}."))
        if self.link is not None:
            await self._check_goals()

        events = await asyncio.to_thread(_get_events)
        filming = self.director is not None and bool(settings.load().get("record_runs"))
        if filming:
            await self._camera(self.director.update(state))

        for evt in events:
            events_log.log("game", fast_brain.describe(evt), type=evt.get("type"),
                           priority=evt.get("priority"))
            self._policy_event(policy_events.from_mod(evt))
            # Рефлекс: локальная LLM выбирает эмоцию, не дожидаясь Claude
            asyncio.create_task(fast_brain.react(evt, state, self._rel))
            if self.link is not None:
                await self._send_game_event(evt)
            if self.commentator is not None and evt.get("type") in ("incident", "pawn_died", "letter", "fire_started"):
                d = evt.get("data", {})
                self.commentator.feed(f"налёт или угроза: {d.get('label')}" if evt.get("type") == "incident"
                                      else f"погиб колонист {d.get('name', '?')}" if evt.get("type") == "pawn_died"
                                      else fast_brain.describe(evt), True)   # письма: срыв, ссора, торговцы, события
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
                self._colony_started = False
                if not self._navigating:
                    asyncio.create_task(self._navigate_to_game(
                        "Колония погибла — все колонисты мертвы. Пройди экран "
                        "окончания игры/статистики RimWorld, вернись в главное "
                        "меню если нужно, начни НОВУЮ колонию (New colony), "
                        "выбери любое подходящее место на карте мира и стартуй игру."))

        critical = [e for e in events if e.get("priority") in ("critical", "high")]
        ticks_since_last = current_tick - self._last_proactive_tick
        threats_now = bool(state.get("threats"))
        battle_over = self._had_threats and not threats_now
        self._had_threats = threats_now
        if threats_now and self.policies.current != "combat":
            n = len(state.get("threats") or [])
            self._policy_event(policy_events.make("raid", f"на карте враги: {n}", {"label": "враги на карте"}))
        if self.policies.current == "combat":          # ТЗ 11e: выход из боя, когда угрозы нет дольше quiet_s
            if threats_now:
                self._threat_gone_since = None
            else:
                self._threat_gone_since = self._threat_gone_since or time.time()
                if time.time() - self._threat_gone_since >= self._exit_quiet_s("combat"):
                    self.policies.finish("угроза миновала")
                    self._threat_gone_since = None
        await self._manual_watch(snap, current_tick)
        await self._apply_speed(threats_now, time.time())
        if battle_over:
            events_log.log("game", "угроза миновала: врагов на карте нет")
        b = claude_budget.get()
        meaningful = False
        if statediff.idle_pawns(snap):
            self._idle_since = self._idle_since or time.time()
        else:
            self._idle_since = 0.0
        self._planning_signals(snap)
        if threats_now and not self._turn_running():
            n = len(state.get("threats") or [])
            agent_mode.set_mode("tactic", f"бой: врагов на карте {n}")
        elif not threats_now and agent_mode.get()["mode"] == "tactic" and not self._turn_running():
            agent_mode.set_mode("observe", "")      # враги исчезли, а хода-итога ещё нет
        if not (critical or battle_over) and ticks_since_last >= PROACTIVE_TICK_INTERVAL:
            since_turn = time.time() - self._last_turn_end
            if since_turn >= (min(b["turn_gap_s"], 20) if threats_now else b["turn_gap_s"]):   # в бою оценка каждые ~20 с
                important = [c for c in statediff.compare(self._turn_snap, snap) if c[0]] if self._turn_snap else []
                idle_long = bool(self._idle_since) and time.time() - self._idle_since >= 120 \
                    and since_turn >= max(b["turn_gap_s"], 240)
                meaningful = (bool(important) or idle_long or self._need_turn or since_turn >= b["turn_idle_s"]
                              or self._turn_snap is None or threats_now)
        should_think = bool(critical) or battle_over or meaningful
        # ТЗ 11e: в бою ход по срезу игры и в бюджете; в планировании обычные ходы молчат (план разложит контур планирования)
        if self.policies.current == "combat" and not (critical or battle_over):
            should_think = bool(threats_now) and self._fight.allow()[0] and pa.combat_ready(
                tick=current_tick, last_turn_tick=self._last_turn_tick,
                slice_ticks=int(self.policies.policy.budget.get("slice_ticks", 600)),
                now=time.time(), last_turn_at=self._last_turn_at)
        elif self.policies.current == "plan" and not critical:
            should_think = False

        if not should_think:
            return

        running = self._turn_task is not None and not self._turn_task.done()
        if running and not critical:
            return                     # ход уже идёт: обычному не мешаем, срочному уступаем место ниже

        hours_passed  = round(ticks_since_last / TICKS_PER_HOUR, 1) if self._last_proactive_tick else None
        self._last_proactive_tick = current_tick
        event_type    = "reactive" if critical else "proactive"
        self._turn_reason = (("угроза: " if any(e.get("type") != "game_loaded" for e in critical) else "")
                             + fast_brain.describe(critical[0])) if critical else (
            "бой окончен, оцениваю итоги" if battle_over else
            "бой продолжается, переоцениваю обстановку" if threats_now else
            "есть колонисты без дела" if (self._idle_since and time.time() - self._idle_since >= 120) else
            "прошлый ход оборвался, повторяю" if self._need_turn else
            "изменения в колонии")
        chat_signals  = list(self._chat_buffer)
        self._chat_buffer.clear()

        just_loaded = self._just_loaded
        self._just_loaded = False
        urgent = [fast_brain.describe(e) for e in critical][:6] if critical else None
        # смерть при загрузке сейва и подобное: тревога только если сейчас реально есть враги
        if urgent and not (state.get("threats") or any(e.get("type") == "pawn_died" for e in critical)):
            urgent = None
        if self.policies.current == "combat" and not urgent and state.get("threats"):
            # бой: каждый ход начинается с паузы и тревоги (игра встаёт на паузу, приказы, потом срез)
            urgent = [f"враги на карте: {len(state.get('threats') or [])}"]
        situation = BATTLE_OVER if battle_over and not urgent else None
        await self._start_turn(current_tick, event_type, hours_passed, chat_signals, just_loaded,
                               urgent=urgent, preempt=bool(critical), situation=situation)

    # ── игровой ход: фоновая задача, которую можно прервать ───────────────────

    async def _start_turn(self, tick, event_type, hours_passed, chat_signals, just_loaded,
                          urgent=None, order=None, preempt=False, situation=None):
        running = self._turn_task is not None and not self._turn_task.done()
        if running:
            # свежий ход (меньше минуты) тревога не обрывает: он и так реагирует
            if not preempt or time.time() - self._turn_started < 60 and self._turn_kind == "reactive" and not order:
                return
            logger.info(f"Ход прерван: срочное дело ({'приказ игрока' if order else 'тревога'})")
            self._turn_task.cancel()
            try:
                await self._turn_task
            except BaseException:
                pass
        self._turn_kind = "reactive" if (urgent or order) else event_type
        self._turn_started = time.time()
        self._turn_task = asyncio.create_task(
            self._turn(tick, event_type, hours_passed, chat_signals, just_loaded, urgent, order, situation))

    def _wake_planning(self, reason: str):
        if self._had_threats or time.time() - self._last_plan_wake < 300:
            return              # в бою не планируем: сейчас важна тактика;                    # не чаще раза в пять минут: иначе она только и делала бы, что планировала
        self._last_plan_wake = time.time()
        self._plan_reason = reason
        events_log.log("game", "режим планирования: " + reason)
        self._wake_planning_policy(reason)           # ТЗ 11e: событие «просьба спланировать» включает политику plan
        self._planning_wake.set()

    def _planning_signals(self, snap: dict):
        """Что она заметила и из-за чего пора спланировать: колонисты без дела, чертежи, которые не строятся,
        и явная просьба самого агента (request_planning)."""
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "plan_request.json")
        try:
            if os.path.exists(req):
                import json
                with open(req, encoding="utf-8") as f:
                    reason = json.load(f).get("reason") or "агент попросил план"
                os.remove(req)
                self._wake_planning("она сама: " + reason)
                return
        except Exception as e:
            logger.warning(f"plan_request: {e}")
        pending = sum(snap["pending"].values())
        if pending and pending == self._pending_seen:
            if time.time() - self._pending_since >= 600:
                self._wake_planning(f"чертежи ({pending}) висят больше десяти минут: возможно, не хватает материала")
        else:
            self._pending_seen, self._pending_since = pending, time.time()
        if self._idle_since and time.time() - self._idle_since >= 300:
            self._wake_planning("колонисты давно без дела: нужны задачи")

    def _turn_running(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def _local_context(self, state: dict, changes) -> str:
        """Короткий контекст для локальной модели (Gemma): возраст колонии, цель, задачи, факты, что изменилось."""
        import colony_intel
        parts = [colony_intel.age_block(state), self._turn_brief(changes), self._ambition_block(), self._tasks_block()]
        return chr(10).join(p for p in parts if p)

    async def _mode_turn(self, mode, state, chat_signals, hours_passed, just_loaded, urgent, order, situation, changes):
        """Ход в режиме (ТЗ 11e): модель задаёт политика (`think`): локальная (Gemma), Claude или смешанно (в бою Claude
        на первом ходе и каждый claude_every-й). Без лимита Claude, если политика не требует его. Подсказка без полного
        снимка мира: события, важное, цель, шаг и список инструментов-запросов."""
        think = self.policies.policy.think
        turn_index = self._fight.turns - 1 if self.policies.current == "combat" else 0
        local = pa.use_local_model(think.get("model", "local"), max(0, turn_index), int(think.get("claude_every", 3)),
                                   claude_blocked=claude_quota.blocked())
        if local:
            import local_agent
            return await local_agent.run_local_turn(self._local_context(state, changes), mode, order, urgent, situation,
                                                    max_steps=int(think.get("max_steps", 8)),
                                                    timeout=float(think.get("timeout_s", 300)))
        return await run_turn(self._context(), chat_signals, hours_passed, just_loaded, urgent=urgent, order=order,
                              situation=situation, changes=None, mode=mode, brief=self._turn_brief(changes))

    async def _turn(self, tick, event_type, hours_passed, chat_signals, just_loaded, urgent, order,
                    situation=None):
        paused_by_us = False
        self._last_turn_tick = self._tick_now
        self._last_turn_at = time.time()
        if self.policies.current == "combat":
            self._fight.spend()                  # ТЗ 11e: бюджет боя (ходов за бой и в минуту)
        fighting = bool(urgent) or self._had_threats
        agent_mode.set_mode("tactic" if fighting else "act",
                            "приказ игрока" if order else (self._turn_reason or "ход"))
        try:
            if urgent:   # как живой игрок: тревога, пауза, приказы, снять паузу
                st = await asyncio.to_thread(_get_state)
                if st and not st.get("paused"):
                    paused_by_us = await asyncio.to_thread(_game_command, {"type": "pause"})
            changes = None
            st_now = await asyncio.to_thread(_get_state)
            self._last_state = st_now
            if st_now and _state_ready(st_now):
                snap_now = statediff.snapshot(st_now)
                changes = statediff.render([c for c in statediff.compare(self._turn_snap, snap_now)
                                            if c[0] and not statediff.is_pause_change(c[1])]) or None   # ТЗ 11e: только важное
                self._turn_snap = snap_now
            logger.info(f"[tick {tick}] GLaDOS думает ({event_type}{', приказ' if order else ''}, +{hours_passed}ч, "
                        f"изменений: {len(changes or [])})…")
            import game_modes
            mode = self.policies.coerce_mode(game_modes.pick_mode(st_now, order or "", urgent, self._tasks_block()))
            game_modes.take_request()                     # старая просьба о смене режима уже неактуальна
            agent_mode.set_mode("tactic" if mode == "combat" else "act", f"режим «{game_modes.MODES[mode].title}»")
            agent_mode.set_game_mode(mode)
            logger.info(f"[tick {tick}] режим хода: {game_modes.MODES[mode].title}")
            result = await self._mode_turn(mode, st_now, chat_signals, hours_passed, just_loaded, urgent, order, situation, changes)
            await self._finish_turn(tick, event_type, result)
            # агент попросил другой режим (switch_mode): новый ход в нём, не больше трёх переходов за цикл
            for _hop in range(3):
                req = game_modes.take_request()
                if not req or req["mode"] == mode:
                    break
                mode = req["mode"]
                logger.info(f"[tick {tick}] переход в режим «{game_modes.MODES[mode].title}»: {req.get('reason', '')}")
                agent_mode.set_mode("tactic" if mode == "combat" else "act", f"режим «{game_modes.MODES[mode].title}»")
                agent_mode.set_game_mode(mode)
                result = await self._mode_turn(mode, st_now, [], None, False, None, None,
                                               f"Ты перешла в режим «{game_modes.MODES[mode].title}»: {req.get('reason', '')}", None)
                await self._finish_turn(tick, event_type, result)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"Ход не удался: {e}", exc_info=True)
        finally:
            self._last_turn_end = time.time()
            if self._had_threats:
                agent_mode.set_mode("tactic", "бой продолжается")
            else:
                agent_mode.set_mode("observe", "")
            if paused_by_us:
                await asyncio.to_thread(_game_command, {"type": "resume"})
            if planning_pause.marked():     # паузу под планирование она поставила и не сняла: игра не должна остаться стоять
                await asyncio.to_thread(_game_command, {"type": "resume"})
                planning_pause.clear()

    async def handle_order(self, text: str) -> bool:
        """Сообщение игрока про игру превращается в приказ: прерывает текущий ход и выполняется сразу.
        True, если приказ принят (игра запущена и фраза про игру)."""
        state = await asyncio.to_thread(_get_state)
        if not _state_ready(state):
            return False
        pawns = [p.get("name", "") for p in (state.get("colony") or {}).get("pawns", [])]
        if not _is_game_talk(text, pawns):
            return False
        logger.info(f"Приказ игрока: {text}")
        events_log.log("game", f"приказ игрока: {text}")
        threats = bool(state.get("threats"))
        urgent = ["В колонии враги на карте"] if threats else None
        await self._start_turn(state.get("tick", 0), "order", None, [], False,
                               urgent=urgent, order=text, preempt=True)
        return True

    async def _finish_turn(self, current_tick: int, event_type: str, result: dict):
        commentary = result.get("commentary", "").strip()
        actions    = result.get("actions", [])
        if result.get("quota"):
            return                     # лимит Claude исчерпан: ничего не говорим и не пишем в журнал решений
        self._need_turn = bool(result.get("timed_out"))   # оборвался: повторить без долгого ожидания
        if result.get("timed_out"):
            events_log.log("error", f"ход в игре прерван по таймауту, сделано вызовов: {len(actions)}")
        elif not actions:
            events_log.log("game", "ход без единого действия")

        commentary = _trim_commentary(commentary)

        _boring = ("отчёт", "тик ", "tick ", "мониторинг", "инициализация")
        _tech = ("localhost", ":8765", "mcp", "api не", "не подключен", "соединение с", "соединение потеряно")
        if any(commentary.lower().startswith(p) for p in _boring):
            commentary = ""
        if commentary and any(t in commentary.lower() for t in _tech):
            logger.info(f"[GLaDOS-тех. сообщение не озвучиваю] {commentary[:80]}")
            commentary = ""    # игра пропала посреди хода: это сбой связи, а не реплика

        if commentary and self._last_commentary and \
                _similarity(commentary, self._last_commentary) > REPEAT_SIMILARITY_MAX:
            logger.info(f"[GLaDOS-повтор подавлен] {commentary}")
            commentary = ""

        if commentary:
            logger.info(f"[GLaDOS] {commentary}")
            self._last_commentary = commentary
            self._say(commentary)
        if actions:
            logger.info(f"[ACTIONS] {actions}")

        self._memory.log_decision(
            tick=current_tick,
            directive=commentary,   # пусто, если реплики не было: никаких заготовок вроде «(молчание)»
            actions=actions,
            event_type=event_type,
        )

    @staticmethod
    def _archive_stale_plans():
        """Новая колония начинает с чистых целей и задач: прошлые заметки («враги на карте», имена
        погибших) сбивают с толку и держат её в обороне. Старое уезжает в memory/archive/."""
        import shutil
        mem = os.getenv("MEMORY_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory")
        dest = os.path.join(mem, "archive", time.strftime("%Y%m%d_%H%M%S"))
        for name in ("goals.json", "tasks.json", "territory.json"):
            src = os.path.join(mem, name)
            if os.path.exists(src):
                os.makedirs(dest, exist_ok=True)
                shutil.move(src, os.path.join(dest, name))
        logger.info("Новая колония: старые цели и задачи убраны в архив")

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
                               text=fast_brain.describe(evt))
        elif kind == "fire_started":
            event = link.event("fire", {"x": d.get("x"), "z": d.get("z")}, text=fast_brain.describe(evt))
        elif kind == "letter" and d.get("letter_def") not in self._COVERED_LETTERS:
            event = link.event("letter", {"label": d.get("label"), "letter_def": d.get("letter_def")},
                               text=fast_brain.describe(evt), urgency=self._LETTER_URGENCY.get(evt.get("priority"), 2))
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
        # TODO(ТЗ 20): если настройка модуля auto_montage включена (self.link.module_settings) и модуль montage подключён,
        # вместо собственного монтажа ниже вызвать core.tool.call("montage.start", {...}) с источником rimworld_run
        try:
            result = await self.recorder.produce(rec)
        except Exception as e:
            logger.error(f"Видео забега: {e}", exc_info=True)
            return
        if result and self.link is not None:
            await self.link.event("run_video", {
                "run_id": result["run_id"], "video": result["video"], "raw": result.get("raw"),
                "scenario": result.get("scenario_path"), "clips": result["clips"]},
                text=("Видео забега готово." if result["video"] else "Запись забега есть, но монтаж не получился."))

    def _ensure_run_started(self, state: dict):
        if self._colony_started:
            return

        colony_name = state.get("colony", {}).get("name", "Unknown Colony")
        save_id     = state.get("save_id", "")
        run         = self._memory.get_current_run()

        if run and save_id and run.get("save_id") != save_id:
            logger.info(f"Обнаружена другая колония/сейв (было: {run.get('colony_name')}, "
                        f"стало: {colony_name}) — завершаю старый эксперимент.")
            self._end_run("session_changed", "Загружен другой сейв")
            run = None

        if not run:
            self._archive_stale_plans()
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
        self._long_term_trigger.set()  # разбудить долгосрочное планирование

    def _say(self, text: str):
        """Любая реплика идёт через компаньона: попадает в чат, в say.json (питомец)
        и в память разговора. Без этого фраза звучит у питомца, но её нет в чате."""
        if self.link is None:
            return
        if settings.quiet_active():
            return   # «не отвлекать»: комментарии игры молчат (в журнале решений они остаются)
        try:
            asyncio.get_running_loop().create_task(self.link.say(text))
        except RuntimeError:
            pass

    # ── Контур 2: тактическое планирование (средний темп) ────────────────────

    async def _planning_loop(self):
        # Небольшая задержка перед первым проходом — чтобы долгосрочные цели
        # (контур 3) успели появиться раньше, есть из чего раскладывать задачи
        await asyncio.sleep(10)
        while True:
            if self.policies.current == "afk":          # ТЗ 11e POL-11: вне фокуса планирование не работает
                self._planning_wake.clear()
                await asyncio.sleep(AFK_SLEEP_S)
                continue
            try:
                if _state_ready(await asyncio.to_thread(_get_state)):
                    logger.info("GLaDOS раскладывает цели на задачи (планирование)…")
                    async with self._background_lock:
                        agent_mode.set_mode("plan", self._plan_reason or "раскладываю цели на задачи")
                        try:
                            result = await (self._plan_cycle() if self.policies.current == "plan"
                                            else run_planning(self._context()))
                        finally:
                            self._plan_reason = ""
                            if not self._turn_running():
                                agent_mode.set_mode("observe", "")
                    if result.get("actions"):
                        logger.info(f"[PLANNING] {result['actions']}")
                        if any("set_task" in a for a in result["actions"]):
                            self._need_turn = True      # задачи розданы: пусть ближайший ход их исполняет
            except Exception as e:
                logger.error(f"Ошибка планирования: {e}", exc_info=True)
            try:
                await asyncio.wait_for(self._planning_wake.wait(), claude_budget.get()["planning_s"])
            except asyncio.TimeoutError:
                pass
            self._planning_wake.clear()

    async def _plan_cycle(self) -> dict:
        """Политика «планирование» (ТЗ 11e, раздел 3.2): пауза, разметка плана Claude, проход самопроверки, снятие паузы,
        выход в наблюдение. Паузу снимаем всегда, даже при сбое: игра не должна остаться стоять."""
        paused = False
        st = await asyncio.to_thread(_get_state)
        if _state_ready(st) and not st.get("paused"):
            paused = bool(await asyncio.to_thread(_game_command, {"type": "pause"}))
            if paused:
                planning_pause.mark()
        result: dict = {}
        try:
            result = await run_planning(self._context())
            if result.get("actions") and self.policies.policy.think.get("self_check", True):
                check = await run_plan_check(self._context(), result)
                result = {**result, "actions": result.get("actions", []) + check.get("actions", [])}
        finally:
            if paused:
                planning_pause.clear()
                await asyncio.to_thread(_game_command, {"type": "resume"})
            self._plan_reason = ""
            self.policies.finish("план разложен и проверен")
        return result

    # ── Контур 3: долгосрочное планирование (медленный темп) ─────────────────

    async def _long_term_planning_loop(self):
        while True:
            if self.policies.current == "afk":          # ТЗ 11e POL-11: вне фокуса контур спит, токены не тратятся
                await asyncio.sleep(AFK_SLEEP_S)
                continue
            try:
                st_goals = await asyncio.to_thread(_get_state)
                if _state_ready(st_goals) and claude_quota.blocked():
                    import local_planner      # Claude спит: стартовый план хотя бы локальной моделью, чтобы цели не были пустыми
                    if await local_planner.seed(st_goals):
                        self._planning_wake.set()
                elif _state_ready(st_goals):
                    logger.info("GLaDOS пересматривает стратегические цели…")
                    async with self._background_lock:
                        result = await run_long_term_planning(self._context())
                    if result.get("actions"):
                        logger.info(f"[LONG-TERM] {result['actions']}")
                    if any("set_goal" in a or "update_goal" in a for a in result.get("actions", [])):
                        self._planning_wake.set()      # цели изменились: сразу разложить их на задачи
            except Exception as e:
                logger.error(f"Ошибка долгосрочного планирования: {e}", exc_info=True)

            self._long_term_trigger.clear()
            try:
                # Ждём обычный интервал, но просыпаемся раньше, если пришло
                # первое состояние новой колонии (см. _ensure_run_started)
                await asyncio.wait_for(self._long_term_trigger.wait(), timeout=claude_budget.get()["longterm_s"])
            except asyncio.TimeoutError:
                pass

    # ── Контур 4: рефлексия (самый медленный темп) ────────────────────────────

    async def _reflection_loop(self):
        while True:
            if self.policies.current == "afk":          # ТЗ 11e POL-11: вне фокуса контур спит, токены не тратятся
                await asyncio.sleep(AFK_SLEEP_S)
                continue
            try:
                run = self._memory.get_current_run()
                decisions = len(run["key_decisions"]) if run else 0
                # без новых решений анализировать нечего: рефлексия не тратит запрос впустую
                fresh = self._refl_seen < 0 or decisions - self._refl_seen >= 3
                if fresh and _state_ready(await asyncio.to_thread(_get_state)):
                    self._refl_seen = decisions
                    logger.info("GLaDOS анализирует историю (рефлексия)…")
                    async with self._background_lock:
                        result = await run_reflection(self._context())
                    if result.get("actions"):
                        logger.info(f"[REFLECTION] {result['actions']}")
            except Exception as e:
                logger.error(f"Ошибка рефлексии: {e}", exc_info=True)
            await asyncio.sleep(claude_budget.get()["reflection_s"])

    # ── Контур 5: самоулучшение (самый редкий, самый мощный) ─────────────────
    # Полный доступ к файлам проекта (Read/Write/Edit/Bash), правит свой же
    # код и код мода. Правки controller.py/glados_agent.py применятся только
    # после перезапуска процесса main.py — сам себя контур не перезапускает.

    async def _self_improvement_loop(self):
        await asyncio.sleep(SELF_IMPROVEMENT_INTERVAL_SECS)  # дать системе поработать, накопить сигнал
        while True:
            if self.policies.current == "afk":          # ТЗ 11e POL-11: вне фокуса самоулучшение не работает
                await asyncio.sleep(AFK_SLEEP_S)
                continue
            try:
                if not claude_budget.get()["selfimp_s"]:
                    await asyncio.sleep(1800)    # в этом режиме расхода самоулучшение выключено
                    continue
                if not _state_ready(await asyncio.to_thread(_get_state)):
                    # Самоулучшение правит код — только пока идёт активная колония,
                    # как раньше (иначе в режиме «всегда включена» она бы правила код вхолостую)
                    await asyncio.sleep(10 * 60)
                    continue
                logger.info("GLaDOS запускает контур самоулучшения…")
                async with self._background_lock:
                    result = await run_self_improvement()
                if result.get("actions"):
                    logger.info(f"[SELF-IMPROVEMENT] {result['actions']}")
            except Exception as e:
                logger.error(f"Ошибка самоулучшения: {e}", exc_info=True)
            await asyncio.sleep(claude_budget.get()["selfimp_s"] or 1800)

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

    async def _navigate_to_game(self, goal: str):
        if self._navigating:
            return
        self._navigating = True
        try:
            import game_launcher
            game_launcher.announce_self(f"веду интерфейс игры (через Claude по экрану) к цели: {goal[:160]}", kind="game")
            logger.info(f"GLaDOS проходит интерфейс игры: {goal[:70]}…")
            async with self._background_lock:
                # Таймаут для навигации — если зависла, откатываем попытку и пробуем позже
                result = await asyncio.wait_for(
                    run_game_navigation(goal),
                    timeout=MENU_STUCK_THRESHOLD_SECS  # 90 сек
                )
            if result.get("actions"):
                logger.info(f"[NAVIGATION] {result['actions']}")
        except asyncio.TimeoutError:
            logger.warning("Навигация по UI превысила таймаут — система перезагружена или зависла")
        except Exception as e:
            logger.error(f"Ошибка навигации по UI: {e}", exc_info=True)
        finally:
            self._navigating = False
