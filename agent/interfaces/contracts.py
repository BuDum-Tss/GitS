"""Контракты (интерфейсы) между сценариями ядра и внешним миром (ТЗ 29, ARC-3, шаг R2).

Сценарии (`Companion`, `Router`, `Attention`) говорят с моделью, журналом, настройками, памятью, людьми и
разрешениями только через эти интерфейсы. Конкретные реализации лежат в `agent/adapters/` (тонкие обёртки над
`fast_brain`, `events_log`, `settings`, `UserMemory`, `EpisodeMemory`, `Relationships`, `approvals`, `decide`).

Здесь только `typing.Protocol` и стандартная библиотека: без `settings`, `events_log`, `fast_brain`, `character`,
сети и баз данных. Проверяет это `test/test_architecture.py`.
"""
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LanguageModel(Protocol):
    """Локальная языковая модель (LM Studio): один текстовый ответ на системную и пользовательскую части."""

    async def ask(self, system: str, user: str, *, timeout: float, max_tokens: int = 40, temperature: float = 0.3,
                  background: bool = False, image_b64: str | None = None) -> str: ...


@runtime_checkable
class StreamingLanguageModel(Protocol):
    """Модель, умеющая отдавать ответ потоком (ТЗ 25, FAST-4): `ask_stream` возвращает асинхронный генератор кусков текста;
    закрытие генератора обрывает запрос. Необязательная способность: без неё ответ приходит целиком."""

    def ask_stream(self, system: str, user: str, *, timeout: float, max_tokens: int = 140,
                   temperature: float = 0.7): ...


@runtime_checkable
class Classifier(Protocol):
    """Классификатор (GLiNER, сервис decider): текст и вопросы -> ответы или None. Исключений наружу не бросает."""

    @property
    def last_ms(self) -> float | None: ...

    async def classify(self, text: str, questions: dict, *, timeout: float) -> dict | None: ...


@runtime_checkable
class EventLog(Protocol):
    """Журнал событий (протокол действий и решений). `log_nowait` пишет в фоне и не ждёт диска."""

    def log(self, kind: str, text: str = "", **data: Any) -> None: ...

    def log_nowait(self, kind: str, text: str = "", **data: Any) -> None: ...


@runtime_checkable
class SettingsProvider(Protocol):
    """Настройки ядра: чтение, изменение и признак режима «не отвлекать»."""

    def load(self) -> dict: ...

    def update(self, **fields: Any) -> dict: ...

    def quiet_active(self, cfg: dict | None = None) -> bool: ...


@runtime_checkable
class MemoryReader(Protocol):
    """Чтение памяти для ответа: эпизоды по теме, воспоминания о себе (канон), журнал разговора и активности.
    `recall` (ТЗ 27) - эпизоды всех видов и дневник через гибридный поиск:
    [{"text", "kind" (episode|canon|note|diary|digest), "id", "importance"}]."""

    def context_block(self, dialogue: int = 6) -> str: ...

    async def topic_block(self, query: str) -> str: ...

    async def canon_block(self, query: str) -> str: ...

    def memory_status(self) -> str | None: ...

    def recent_dialogue(self, n: int = 8) -> list[dict]: ...

    def recent_activity(self, n: int = 5) -> list[dict]: ...

    async def recall(self, query: str, k: int = 6) -> list[dict]: ...

    def meta(self, key: str, default: Any = None) -> Any: ...


@runtime_checkable
class MemoryWriter(Protocol):
    """Запись в память: реплики, важное (заметки эпизодов, планы как задачи), активность,
    забывание, служебные отметки. `add_fact` берёт только важное (заметка от 3, план от 3): мелочь не записывается."""

    def add_dialogue(self, role: str, text: str) -> None: ...

    def add_fact(self, text: str, kind: str = "user", importance: int = 3, due_days: float | None = None) -> bool: ...

    def add_activity(self, app: str, category: str, summary: str) -> None: ...

    def forget_matching(self, text: str) -> int: ...

    def set_meta(self, key: str, value: Any) -> None: ...


@runtime_checkable
class PeopleDirectory(Protocol):
    """Люди, с которыми общается ядро: профиль, симпатия, заметки. Без профиля все методы безопасны (пусто, 0, None)."""

    def ensure_person(self, key: str, name: str, source: str = "", **extra: Any) -> bool: ...

    def note(self, key: str, text: str) -> None: ...

    def get(self, subject: str) -> dict | None: ...

    def describe(self, subject: str) -> str: ...

    def adjust(self, subject: str, delta: float, reason: str) -> float: ...

    def sync_colonists(self, pawns: list[dict]) -> None: ...

    def mark_dead(self, subject: str, cause: str = "") -> None: ...


@runtime_checkable
class Embedder(Protocol):
    """Эмбеддер текстов (EmbeddingGemma в LM Studio): тексты -> векторы; query отличает запрос от документа."""

    async def embed(self, texts: list[str], query: bool = False) -> list[list[float]]: ...


@runtime_checkable
class Clock(Protocol):
    """Время в секундах (time.time по умолчанию, в тестах подменяется)."""

    def __call__(self) -> float: ...


@runtime_checkable
class Approver(Protocol):
    """Разрешения и вопросы пользователю: есть ли открытый вопрос, разрешение на действие, вопрос с ответом,
    принятие ответа на открытый вопрос, идёт ли сейчас формулировка вопроса."""

    def has_pending(self) -> bool: ...

    def formulating(self) -> bool: ...

    async def request(self, text: str, announce: Any, danger: str | None = None) -> bool: ...

    async def ask(self, kind: str, question: str, options: list[str] | None, timeout_s: float, source: str, *,
                  announce: Any = None, context: dict | None = None) -> tuple[str, str]: ...

    async def try_answer(self, text: str) -> bool: ...


@runtime_checkable
class ToolInvoker(Protocol):
    """Выполнение инструмента по id с обычным контролем (разрешения, риск). Возвращает (ok, результат, код ошибки)."""

    async def call(self, tool_id: str, args: dict) -> tuple[bool, str, str | None]: ...


@runtime_checkable
class ReminderRepository(Protocol):
    """Напоминания, которые агент назначает себе (ТЗ 30, IMP-2). Запись: dict с полями id, due, text, source, status
    (`pending` / `fired` / `cancelled`), depth, untrusted, created, ttl_s."""

    def add(self, due: float, text: str, *, source: dict | None = None, depth: int = 0, untrusted: bool = False,
            ttl_s: float | None = None) -> dict: ...

    def get(self, reminder_id: str) -> dict | None: ...

    def update(self, reminder_id: str, *, due: float | None = None, text: str | None = None,
               priority: float | None = None) -> dict | None:
        ...

    def pending(self) -> list[dict]: ...

    def set_status(self, reminder_id: str, status: str) -> None: ...


@runtime_checkable
class TaskRepository(Protocol):
    """Хранилище общих задач (ТЗ 32, WS-8). Задача это dict в виде `Task.to_dict()` (домен `services/tasks/model.py`): id, workspace, goal,
    parent, steps, status, origin, priority, due, created, updated, checkpoint, kind, meta. Методы блокирующие; зовите их
    через `asyncio.to_thread`."""

    def save(self, task: dict) -> None: ...

    def save_review(self, snapshot: dict, task: dict, open_statuses: tuple[str, ...]) -> bool: ...

    def get(self, task_id: str) -> dict | None: ...

    def list(self, *, workspace: str | None = None, statuses: tuple[str, ...] | None = None, kind: str | None = None,
             parent: str | None = None) -> list[dict]: ...

    def delete(self, task_id: str) -> None: ...


@runtime_checkable
class Planner(Protocol):
    """Разбиение цели на шаги (ТЗ 32, WS-8): `plan(goal, workspace_ctx)` -> [{"text": шаг}], по порядку. Универсально для всех
    сред; `workspace_ctx` (среда, её назначение, инструменты, уже сделанное) только подсказка. Сбой модели -> пустой список."""

    async def plan(self, goal: str, workspace_ctx: dict) -> list[dict]: ...


@runtime_checkable
class EventSource(Protocol):
    """Журнал событий для эпизодов (ТЗ 27): строки с номером `id` после заданного, последний номер, удаление сырого по сроку."""

    def after(self, after_id: int = 0, limit: int = 500, kinds: set | None = None) -> list[dict]: ...

    def last_id(self) -> int: ...

    def delete_ids(self, ids: list[int]) -> int: ...


@runtime_checkable
class EventSink(Protocol):
    """Приёмник сообщений модулей (ТЗ 29, ARC-7): шина модулей передаёт сюда события, речь, эмоции, обновления задач и
    вопросы задач, ничего не зная о менеджере внимания и модели. Реализация в ядре: `adapters/module_events.py`."""

    async def submit(self, peer_id: str, event: dict) -> None: ...

    async def submit_speech(self, peer_id: str, say: dict) -> None: ...

    async def on_job_update(self, peer_id: str, job: dict) -> None: ...

    async def submit_question(self, peer_id: str, job_id: str, question_id: str, question: dict) -> tuple[Any, str]: ...


@runtime_checkable
class EventAttention(Protocol):
    """Сервис внимания событий (ТЗ 03; реализация `attention.Attention`, папка `agent/attention/`): единственное, что остальное
    ядро знает о внимании. Приём событий, речи, вопросов и ввода пользователя; очередь речи; временные правила; чтение
    сводки и состояния; подключение при сборке ядра. Внутренние файлы папки наружу не импортируются (`test_attention_boundary.py`)."""

    decider: Any

    # приём
    async def submit(self, peer_id: str, event: dict) -> None: ...

    async def submit_speech(self, peer_id: str, say: dict) -> None: ...

    async def submit_announce(self, peer_id: str, text: str, *, data: dict | None = None) -> None: ...

    async def submit_question(self, q: Any) -> Any: ...

    async def submit_self(self, event: Any) -> None: ...

    async def on_job_update(self, peer_id: str, job: dict) -> None: ...

    # ввод пользователя и речь
    async def on_user_input(self, text: str) -> None: ...

    async def classify_input(self, text: str) -> Any: ...

    def set_user_speaking(self, speaking: bool) -> None: ...

    def looks_like_stop(self, text: str) -> bool: ...

    def enqueue_say(self, text: str, sticker: str | None = None, priority: int | None = None, **kwargs: Any) -> Any: ...

    def enqueue_fact(self, fact: str, user_text: str = "", must: bool = False, question: bool = False) -> Any: ...

    async def stop_speech(self, why: str = "stop") -> int: ...

    def discard_speech(self, cand: Any, why: str = "cancelled") -> None: ...

    # временные правила
    async def set_rule(self, *, module: str | None = None, kind: str | None = None, sender: str | None = None,
                       effect: str, minutes: float = 60) -> str: ...

    def rules_text(self) -> str: ...

    async def clear_rules(self, rule_id: str | None = None) -> str: ...

    # чтение состояния
    def snapshot(self) -> dict: ...

    def buffer_view(self) -> dict: ...

    def context_events(self, limit: int = 6) -> str: ...

    def digest_counts(self) -> dict[str, int]: ...

    def take_digest(self, module: str | None = None) -> str: ...

    def top_focus(self) -> Any: ...

    def is_silent(self, kind: str) -> bool: ...

    def settings_now(self) -> dict: ...

    def quiet_now(self) -> bool: ...

    # размышление и смена персонажа
    def attach_reflection(self, service: Any) -> None: ...

    async def reflect_speech(self, event: Any, intent: Any) -> str: ...

    # подключение при сборке ядра
    def connect(self, *, hub: Any = None, modules: Any = None, profiles: Any = None) -> None: ...

    def set_node_watch(self, fn: Any) -> None: ...

    def set_env_of(self, fn: Any) -> None: ...

    def set_think_handler(self, fn: Any) -> None: ...

    def set_alert_handler(self, fn: Any) -> None: ...

    def set_reply_handler(self, fn: Any) -> None: ...

    def register_handler(self, kind: str, handler: Any) -> None: ...

    def register_handlers(self, handlers: dict) -> None: ...

    def register_hook(self, kind: str, hook: Any) -> None: ...

    def add_silent_kinds(self, kinds: Any) -> None: ...

    def spawn(self, coro: Any) -> Any: ...

    # жизненный цикл
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def settle(self) -> None: ...


@runtime_checkable
class RequestHandler(Protocol):
    """Обработчик запросов модуля к ядру `core.*` (шина зовёт его для каждого запроса). Ошибки: `PeerError`."""

    async def handle(self, conn: Any, msg_type: str, body: dict) -> Any: ...


# ── среда исполнения единого агента ────────────────────────────────────────────────────────

ToolHandler = Callable[[dict], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class ToolSpec:
    """Инструмент ядра для единого агента: имя, описание, параметры (JSON Schema) и обработчик `args -> (успех, текст)`.
    Описывается один раз и отдаётся любой среде исполнения; обработчик не бросает наружу ничего, кроме отмены."""
    name: str
    description: str
    parameters: dict
    handler: ToolHandler


@dataclass(frozen=True)
class TextEvent:
    """Целое сообщение модели (кусок речи)."""
    text: str


@dataclass(frozen=True)
class ToolEvent:
    """Агент вызывает инструмент (для журнала; исполняет его ядро через `ToolSpec.handler`)."""
    name: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EndEvent:
    """Ход закончен, ровно один раз на ход: `status` ok | aborted | error, `error` текст ошибки, `usage` расход."""
    status: str = "ok"
    error: str | None = None
    usage: dict = field(default_factory=dict)


RuntimeEvent = TextEvent | ToolEvent | EndEvent


@runtime_checkable
class AgentRuntime(Protocol):
    """Среда исполнения единого агента (pi через мост Node). Держит одну постоянную сессию.

    `start` запоминает системную подсказку и инструменты (вызывается перед каждым ходом, повтор безвреден; подсказка,
    изменившаяся между ходами, действует со следующего запроса или новой сессии). `run` ведёт один ход и отдаёт события;
    последним всегда приходит `EndEvent`. Сбой среды поднимается исключением или завершает ход `EndEvent` со
    статусом error. `steer` подмешивает сообщение в идущий ход: True принято, False хода нет или среда не умеет.
    `abort` обрывает идущий ход. `reset(forget=True)` забывает историю, `forget=False` только закрывает сессию, чтобы её
    подхватил следующий запуск. `close` освобождает ресурсы."""

    can_steer: bool

    async def start(self, system: str, tools: list[ToolSpec]) -> None: ...

    def run(self, prompt: str) -> AsyncIterator[RuntimeEvent]: ...

    async def steer(self, text: str) -> bool: ...

    async def abort(self) -> None: ...

    async def reset(self, forget: bool = True) -> None: ...

    async def close(self) -> None: ...
