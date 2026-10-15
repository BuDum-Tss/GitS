"""Общее для компонентов внимания (ТЗ 29, ARC-4): константы, данные события и общий контекст.

`AttentionContext` держит то, что делят все компоненты и что ядро подставляет после создания (`hub`, `modules`, `profiles`,
`on_decided`, `policy_options`, `kind_handlers`...). Компоненты читают поля контекста при каждом вызове, поэтому подмена
после создания (в ядре и в тестах) работает.
"""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import settings

logger = logging.getLogger("attention")

TEXT_LOG = 200                       # сколько символов текста уходит в журнал (ATT-29)
TEXT_MAX = 1000
CLASSIFIER_QUEUE_MAX = 20            # ATT-32
TICK_S = 0.5
RULES_PURGE_S = 60.0
DIGEST_MAX = 50                      # ATT-26
DIGEST_SPEECH_MAX = 5                # ATT-27
DIGEST_MIN_ITEMS, DIGEST_MIN_PRIORITY, DIGEST_QUIET_S = 3, 25, 600.0
CONVERSATION_FOCUS_P, CONVERSATION_IDLE_S, RETURN_WINDOW_S, RETURN_P = 90, 60.0, 300.0, 40
FOCUS_DEPTH = 3                      # ATT-21
REACTION_MAX_AGE_S = 900.0
SPEECH_DEFAULT_URGENCY = 2           # речь модуля без urgency_hint
PROACTIVE_P = 30                     # проактивная реплика - «болтовня»: звучит, когда больше нечего сказать
CONVERSATION_P = 95                  # ATT-13
QUESTION_ANNOUNCE_P = 80             # формулировка вопроса разрешения (approvals.request), сказанная вне submit_question
ANNOUNCE_DEFAULT_URGENCY = 3
DEFAULT_HALF_LIFE = {"rimworld.*": 60, "telegram.*": 900, "desktop.*": 120, "*.job": 600, "*": 300}
DEFAULT_THRESHOLDS = (15, 35, 60, 85)
REPLY_CATEGORIES = ("question", "request_to_assistant")      # категории, в которых ответа ждут сами по себе


@dataclass
class Question:
    """Вопрос пользователю: от модуля (core.ask), из задачи модуля (waiting_user) или от самого ядра."""

    kind: str                                   # approval | choice | text
    question: str                               # факт: о чём спросить; формулировку вслух строит ядро
    options: list[str] | None = None
    timeout_s: float = 300
    source: str = "core"                        # id модуля или "core"
    context: dict = field(default_factory=dict)
    urgency_hint: int | None = None
    job_id: str | None = None                   # для вопросов задач
    question_id: str | None = None


@dataclass
class Item:
    """Событие внутри менеджера (ATT: раздел 4)."""

    id: str                                     # "a-" + 8 hex
    module: str
    kind: str                                   # "telegram.dm"
    text: str                                   # факт
    data: dict
    sender: dict | None
    reply_channel: dict | None
    addressed_to_me: bool | None
    urgency: int | None                         # 0..5 после правил и классификатора
    needs_reply: bool
    memorable: bool
    created: float
    expires: float | None
    dedupe_key: str | None
    count: int = 1                              # склеенные повторы
    urgency_hint: int | None = None
    updated: float = 0.0
    spoken: bool = False
    priority: float = 0.0
    decision: str = ""
    first_contact: bool = False                 # TG-14: профиль отправителя заведён именно этим событием (для веса «неизвестный»)
    memory_done: bool = False                   # TG-14: событие уже отдано в граф и память, в _act второй раз не отдаётся
    trace: Any = None                           # трасса задержки голосовой реплики (ТЗ 24, TRC-1): от модуля, см. latency_trace


@dataclass
class Focus:
    id: str
    kind: str                                   # "idle" | "conversation" | "reaction" | "question"
    topic: str                                  # факт: о чём
    source: str
    priority: int
    started: float
    last_active: float
    resumable: bool


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class NullSpeech:
    """Вывод речи, которого нет (тесты без компаньона)."""

    async def speak(self, utterance_id, text, emotion, priority) -> None: ...
    async def stop(self, utterance_id=None) -> None: ...
    def is_speaking(self) -> bool:
        return False
    def current_segment(self) -> str:
        return ""


class NullPresence:
    def __init__(self):
        self.state = {"speaking": False, "user_speaking": False, "last_user_ts": 0.0, "focus": "idle", "busy": False}

    def snapshot(self) -> dict:
        return dict(self.state)

    def update(self, **fields) -> bool:
        self.state.update(fields)
        return True


class AttentionContext:
    """Общие зависимости и позднее подставляемые поля менеджера внимания (см. docstring модуля)."""

    def __init__(self, *, companion, io, clock, decider, settings_provider, settings_source, log, approver, people,
                 writer, idle, autopump, rules, presence):
        self.companion = companion
        self.io = io
        self.clock = clock
        self.decider = decider
        self.settings_provider = settings_provider
        self.settings = settings_source
        self.log = log
        self.approver = approver
        self.people = people
        self.writer = writer
        self.idle = idle
        self.autopump = autopump
        self.rules = rules
        self.presence = presence
        self.hub = None                          # ModuleHub: соединения для доставки вопросов, ставит ядро
        self.modules = None                      # ModuleRegistry: манифесты, включённость
        self.profiles = None                     # ProfileManager: веса профиля
        # служебные события модулей, которые ядро обрабатывает само и не озвучивает: kind -> async (Item) -> None
        self.kind_handlers: dict[str, Callable] = {}
        # то же для событий, которым нужно побочное действие, но которые дальше идут обычным путём внимания
        self.kind_hooks: dict[str, Callable] = {}
        # менеджер фокуса (ТЗ 18): on_decided(item, decision, priority, rule), синхронный; None - фокуса нет
        self.on_decided: Callable[[Item, str, float, str | None], None] | None = None
        # ТЗ 18, первый эшелон: варианты подсказки политики для классификатора; None - не спрашиваем
        self.policy_options: Callable[[], dict] | None = None
        self.silent_kinds: set[str] = set()      # виды, которые не пишутся в журнал module_event
        self.last_decision: dict | None = None   # для отладки и дашборда
        self.decisions: deque = deque(maxlen=100)
        self.last_user_text = ""
        self.tasks: set[asyncio.Task] = set()

    # ── фоновые задачи ───────────────────────────────────────────────────────

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"Фоновая задача внимания: {task.exception()!r}")

    # ── настройки и названия ─────────────────────────────────────────────────

    def att(self, cfg: dict | None = None) -> dict:
        """Настройки attention с проверкой значений: испорченное заменяется значением по умолчанию."""
        raw = (cfg if cfg is not None else self.settings()).get("attention")
        raw = raw if isinstance(raw, dict) else {}
        d = settings.DEFAULTS["attention"]
        out = {**d, **raw}
        th = out.get("thresholds")
        if not (isinstance(th, (list, tuple)) and len(th) == 4 and all(isinstance(x, (int, float)) for x in th)
                and list(th) == sorted(th)):
            out["thresholds"] = list(DEFAULT_THRESHOLDS)
        for key in ("interrupt_margin", "away_after_s", "min_conf_event", "min_conf_input"):
            if not isinstance(out.get(key), (int, float)) or isinstance(out.get(key), bool):
                out[key] = d[key]
        if not isinstance(out.get("half_life"), dict) or not out["half_life"]:
            out["half_life"] = dict(DEFAULT_HALF_LIFE)
        out["classifier"] = bool(out.get("classifier", True))
        return out

    def module_title(self, module_id: str) -> str:
        rec = self.modules.get(module_id) if self.modules is not None else None
        return (rec.title if rec is not None and rec.title else module_id)

    def emit_info(self, module_id: str, kind: str) -> dict | None:
        rec = self.modules.get(module_id) if self.modules is not None else None
        for e in ((rec.manifest or {}).get("emits") or []) if rec is not None else []:
            if isinstance(e, dict) and e.get("kind") == kind:
                return e
        return None

    @staticmethod
    def sender_name(sender: dict | None) -> str | None:
        return (sender or {}).get("name") or (sender or {}).get("id")

    def quiet(self, cfg: dict) -> bool:
        return bool(cfg.get("dnd")) or self.clock() < float(cfg.get("quiet_until") or 0.0)
