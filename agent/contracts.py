"""Контракты (интерфейсы) между сценариями ядра и внешним миром (ТЗ 29, ARC-3, шаг R2).

Сценарии (`Companion`, `Router`, `Attention`) говорят с моделью, журналом, настройками, памятью, людьми и
разрешениями только через эти интерфейсы. Конкретные реализации лежат в `agent/adapters/` (тонкие обёртки над
`fast_brain`, `events_log`, `settings`, `UserMemory`, `GraphMemory`, `Relationships`, `approvals`, `decide`).

Здесь только `typing.Protocol` и стандартная библиотека: без `settings`, `events_log`, `fast_brain`, `character`,
сети и баз данных. Проверяет это `agent/tests/test_architecture.py`.
"""
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
    """Чтение памяти для ответа: профиль владельца, факты графа по теме, воспоминания о себе, журнал
    разговора и активности. `recall` (ТЗ 27) - факты по теме и эпизоды с дневником через гибридный поиск:
    [{"text", "kind" (fact|episode|diary|digest), "id", "importance"}]."""

    def context_block(self, dialogue: int = 6) -> str: ...

    def profile_block(self) -> str: ...

    async def graph_block(self, query: str) -> str: ...

    async def self_block(self, query: str) -> str: ...

    def graph_status(self) -> str | None: ...

    def recent_dialogue(self, n: int = 8) -> list[dict]: ...

    def recent_activity(self, n: int = 5) -> list[dict]: ...

    def facts(self) -> list[dict]: ...

    async def recall(self, query: str, k: int = 6) -> list[dict]: ...

    def meta(self, key: str, default: Any = None) -> Any: ...


@runtime_checkable
class MemoryWriter(Protocol):
    """Запись в память: реплики, важное (профиль владельца, планы как задачи), активность, очередь графа,
    забывание, служебные отметки. `add_fact` берёт только важное (4-5, план от 3): мелочь не записывается."""

    def add_dialogue(self, role: str, text: str) -> None: ...

    def add_fact(self, text: str, kind: str = "user", importance: int = 3, due_days: float | None = None) -> bool: ...

    def add_activity(self, app: str, category: str, summary: str) -> None: ...

    def enqueue_for_graph(self, speaker: str, text: str, source: str | None = None, owner: bool = False) -> None: ...

    def forget_matching(self, text: str) -> int: ...

    def set_meta(self, key: str, value: Any) -> None: ...

    def rebind(self, memory_dir: Any) -> None: ...


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
class FactGraph(Protocol):
    """Граф фактов (SQLite, fact_store.py): поиск, факты о себе, очередь записи, колонисты, состояние."""

    def search(self, query: str, limit: int = 6, include_old: bool = False) -> Any: ...

    def self_memories(self, query: str, n: int = 6) -> Any: ...

    def context_block(self, query: str) -> Any: ...

    def enqueue(self, speaker: str, text: str, ts: float | None = None) -> None: ...

    def sync_colonists(self, pawns: list[dict], colony: str, force: bool = False) -> Any: ...

    def status_text(self) -> str: ...


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

    def pending(self) -> list[dict]: ...

    def set_status(self, reminder_id: str, status: str) -> None: ...


@runtime_checkable
class TaskRepository(Protocol):
    """Хранилище общих задач (ТЗ 32, WS-8). Задача это dict в виде `Task.to_dict()` (домен `domain/tasks.py`): id, workspace, goal,
    parent, steps, status, origin, priority, due, created, updated, checkpoint, kind, meta. Методы блокирующие; зовите их
    через `asyncio.to_thread`."""

    def save(self, task: dict) -> None: ...

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
class ThoughtJournal(Protocol):
    """Журнал мыслей (ТЗ 30, IMP-6): записи не озвучиваются."""

    def append(self, thought: dict) -> None: ...

    def recent(self, n: int = 50) -> list[dict]: ...

    def update(self, thought_id: str, **fields: Any) -> bool: ...


@runtime_checkable
class EventSink(Protocol):
    """Приёмник сообщений модулей (ТЗ 29, ARC-7): шина модулей передаёт сюда события, речь, эмоции, обновления задач и
    вопросы задач, ничего не зная о менеджере внимания и модели. Реализация в ядре: `adapters/module_events.py`."""

    async def submit(self, module_id: str, event: dict) -> None: ...

    async def submit_speech(self, module_id: str, say: dict) -> None: ...

    def on_emotion(self, module_id: str, emotion: Any) -> bool: ...

    async def on_job_update(self, module_id: str, job: dict) -> None: ...

    async def submit_question(self, module_id: str, job_id: str, question_id: str, question: dict) -> tuple[Any, str]: ...


@runtime_checkable
class RequestHandler(Protocol):
    """Обработчик запросов модуля к ядру `core.*` (шина зовёт его для каждого запроса). Ошибки: `ModuleError`."""

    async def handle(self, conn: Any, msg_type: str, body: dict) -> Any: ...
