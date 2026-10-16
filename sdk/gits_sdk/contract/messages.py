"""Все сообщения контракта «ядро - модуль» (ТЗ 31, MC-1): запросы модуля к ядру, запросы ядра к модулю, сообщения без
ответа, темы. Это единственное место, где записаны поля, ограничения, коды ошибок, статус и версия появления.

Тело `event`, `say`, `core.ask`, `job.update`, `panel.set`, `hello` проверяют готовые `validate_*` протокола
(`gits_sdk.protocol`): у таких сообщений поля перечислены здесь для документа и схем, а проверку ведёт одна функция.
"""
from __future__ import annotations

from .. import protocol as P
from .spec import C2M, M2C, NOTE, PLANNED, REQUEST, TOPIC, Field, Message

F = Field

TTS_TEXT_MAX = 5000
LLM_TEXT_MAX = 60_000
LLM_IMAGE_B64_MAX = 700_000              # картинка укладывается в сообщение 1 МиБ (MOD-6)
LLM_TIMEOUT_MAX_S = 120
LLM_TOKENS_MAX = 2000
LOG_TEXT_MAX = 500
LOG_KINDS = ("error", "telegram", "hearing", "vision", "approval", "game", "memory")   # что модуль вправе писать в журнал
LOG_READ_KINDS = ("game", "telegram", "hearing", "vision", "error")                    # что модуль вправе читать из журнала
TAIL_LIMIT_MAX = 100

_OBJ_OK = ({"x": 1},)

# ── запросы модуля к ядру ─────────────────────────────────────────────────────

_ask = Message(
    "core.ask", M2C, REQUEST, "Вопрос пользователю через менеджер внимания. Ответ: bool для approval, строка для choice и text.",
    request=(F("kind", "string", True, "вид вопроса", enum=P.ASK_KINDS),
             F("question", "string", True, "текст вопроса", nonempty=True),
             F("options", "array", False, "варианты (для choice обязательны)", items="string"),
             F("timeout_s", "number", False, "тайм-аут ответа, секунд", min=1, max=P.ASK_TIMEOUT_MAX,
               default=P.ASK_TIMEOUT_DEFAULT),
             F("context", "object", False, "любые данные для показа вместе с вопросом"),
             F("urgency_hint", "integer", False, "подсказка срочности", min=P.URGENCY_MIN, max=P.URGENCY_MAX)),
    response=(F("answer", "any", True, "ответ пользователя (по тайм-ауту false или null)"),
              F("by", "string", False, "кто ответил (канал)")),
    errors=("bad_request", "unavailable", "timeout"),
    check=P.validate_ask,
    valid=({"kind": "approval", "question": "Запустить сборку?"},
           {"kind": "choice", "question": "Что делаем?", "options": ["да", "нет"], "timeout_s": 30},
           {"kind": "text", "question": "Имя файла?", "urgency_hint": 2}),
    invalid=({}, {"kind": "yesno", "question": "?"}, {"kind": "approval", "question": "   "},
             {"kind": "choice", "question": "?"}, {"kind": "approval", "question": "?", "timeout_s": 0}, "не объект"),
    response_valid=({"answer": True, "by": "dashboard"}, {"answer": None}),
    response_invalid=({},),
)

_tts = Message(
    "core.tts", M2C, REQUEST, "Синтез речи её голосом: ядро передаёт запрос модулю голоса (`tts.synthesize`) и его ответ.",
    request=(F("text", "string", True, "что произнести", nonempty=True, max=TTS_TEXT_MAX),
             F("emotion", "string", False, "эмоция реплики"),
             F("format", "string", False, "формат файла", enum=("wav", "ogg"), default="wav")),
    response=(F("path", "string", True, "путь к звуковому файлу"),
              F("duration_ms", "integer", True, "длительность, мс", min=0)),
    errors=("bad_request", "unavailable", "timeout", "internal"),
    valid=({"text": "Привет"}, {"text": "Привет", "emotion": "joy", "format": "ogg"}),
    invalid=({}, {"text": ""}, {"text": "x", "format": "mp3"}, {"text": "x" * (TTS_TEXT_MAX + 1)}, {"text": 5}),
    response_valid=({"path": "C:/tmp/a.wav", "duration_ms": 1200},),
    response_invalid=({"path": "a.wav"}, {"duration_ms": 1}, {"path": 1, "duration_ms": 1}),
)

_tool_call = Message(
    "core.tool.call", M2C, REQUEST, "Вызов инструмента любого окружения ядра (кроме запрещённых правилами) по id `модуль.имя`.",
    request=(F("tool", "string", True, "id инструмента вида модуль.имя", nonempty=True),
             F("args", "object", False, "аргументы инструмента")),
    response=(F("text", "string", True, "результат инструмента"),),
    errors=("bad_request", "unknown", "unavailable", "denied", "internal"),
    valid=({"tool": "desktop.screenshot"}, {"tool": "echo.repeat", "args": {"text": "а"}}),
    invalid=({}, {"tool": ""}, {"tool": "x", "args": [1]}),
    response_valid=({"text": "готово"},),
    response_invalid=({},),
)

_lookup = Message(
    "core.lookup", M2C, REQUEST, "Справочник знаний ядра: поиск по коллекциям базы знаний (монтаж берёт так стиль и опыт).",
    request=(F("query", "string", True, "что искать", nonempty=True),
             F("collections", "array", False, "коллекции, например montage.style", items="string"),
             F("allow_web", "boolean", False, "можно ли дополнять поиском в сети", default=False),
             F("k", "integer", False, "сколько результатов", min=1, max=50)),
    response=(F("text", "string", True, "найденное одним текстом"),),
    errors=("bad_request", "unavailable"), status=PLANNED, planned_in="ТЗ 04", note="Пока отвечает unavailable.",
    valid=({"query": "кофе"}, {"query": "стиль", "collections": ["montage.style"], "allow_web": False, "k": 5}),
    invalid=({}, {"query": ""}, {"query": "x", "k": 0}),
    response_valid=({"text": "..."},), response_invalid=({},),
)
_recall = Message(
    "core.memory.recall", M2C, REQUEST, "Вспомнить: поиск по памяти (контракт `MemoryReader`).",
    request=(F("query", "string", True, "о чём вспомнить", nonempty=True), F("limit", "integer", False, "сколько фактов", min=1, max=50)),
    response=(F("facts", "array", True, "факты", items="object"),),
    errors=("bad_request", "unavailable"), status=PLANNED, planned_in="ТЗ 04", note="Пока отвечает unavailable.",
    valid=({"query": "день рождения"},), invalid=({}, {"query": " "}),
    response_valid=({"facts": []},), response_invalid=({},),
)
_remember = Message(
    "core.memory.remember", M2C, REQUEST, "Запомнить факт (контракт `MemoryWriter`).",
    request=(F("text", "string", True, "что запомнить", nonempty=True), F("source", "string", False, "откуда факт")),
    response=(F("stored", "boolean", True, "записано ли"),),
    errors=("bad_request", "unavailable"), status=PLANNED, planned_in="ТЗ 04", note="Пока отвечает unavailable.",
    valid=({"text": "любит чай"},), invalid=({}, {"text": ""}),
    response_valid=({"stored": True},), response_invalid=({},),
)
_exp_add = Message(
    "core.experience.add", M2C, REQUEST, "Добавить опыт модуля в память: вывод (`statement`) с областью (`scope`) и контекстом.",
    request=(F("scope", "string", True, "область опыта, например montage", nonempty=True),
             F("statement", "string", True, "вывод одной фразой", nonempty=True),
             F("context", "string", False, "в каком случае это верно"),
             F("data", "object", False, "подробности")),
    response=(F("stored", "boolean", False, "записано ли"),),
    errors=("bad_request", "unavailable"), status=PLANNED, planned_in="ТЗ 04", note="Пока отвечает unavailable.",
    valid=({"scope": "montage", "statement": "короткие сцены держат внимание", "context": "ролики до минуты"},),
    invalid=({}, {"scope": "montage"}, {"scope": "m", "statement": 3}),
    response_valid=({"stored": True}, {}), response_invalid=({"stored": "да"},),
)
_kb_ingest = Message(
    "core.kb.ingest", M2C, REQUEST, "Отдать документы в коллекцию базы знаний (`replace` заменяет коллекцию, `final` завершает загрузку).",
    request=(F("collection", "string", True, "коллекция, например montage.style", nonempty=True),
             F("documents", "array", True, "документы: объекты с текстом", items="object"),
             F("replace", "boolean", False, "заменить коллекцию целиком", default=False),
             F("final", "boolean", False, "последняя порция", default=False)),
    response=(F("stored", "integer", False, "сколько документов принято", min=0),),
    errors=("bad_request", "unavailable"), status=PLANNED, planned_in="ТЗ 04", note="Пока отвечает unavailable.",
    valid=({"collection": "montage.style", "documents": [{"title": "Правила", "text": "..."}], "replace": True},),
    invalid=({}, {"collection": "a"}, {"collection": "a", "documents": "x"}),
    response_valid=({"stored": 2}, {}), response_invalid=({"stored": -1},),
)

_persona = Message(
    "core.persona", M2C, REQUEST, "Карточка активного персонажа и разделы persona.md для текстов модуля (ТЗ 05).",
    request=(),
    response=(F("id", "string", True, "id персонажа"), F("name", "string", True, "имя"),
              F("aliases", "array", False, "другие имена", items="string"), F("gender", "string", False, "род"),
              F("language", "string", False, "язык"), F("world", "string", False, "мир"),
              F("voice", "object", False, "параметры голоса"), F("card", "string", False, "краткая карточка характера"),
              F("sections", "object", False, "разделы persona.md")),
    errors=("unavailable", "internal"),
    valid=({},),
    invalid=("не объект",),
    response_valid=({"id": "glados", "name": "ГЛаДОС", "card": "", "sections": {}},),
    response_invalid=({"id": "glados"},),
)

_privacy = Message(
    "core.privacy_terms", M2C, REQUEST, "Личные слова для фильтров публикации (TG-12): имя владельца, имена знакомых.",
    request=(),
    response=(F("terms", "array", True, "слова", items="string"),),
    errors=("unavailable", "internal"),
    valid=({},), invalid=("не объект",),
    response_valid=({"terms": ["Аня"]}, {"terms": []}),
    response_invalid=({}, {"terms": [1]}),
)

_claude_usage = Message(
    "core.claude_usage", M2C, REQUEST, "Учесть расход токенов Claude модулем и, если лимит исчерпан, сообщить его текст.",
    request=(F("tokens", "integer", True, "расход токенов", min=0), F("source", "string", False, "кто потратил"),
             F("limit_text", "string", False, "текст исчерпанного лимита")),
    response=(),
    errors=("bad_request",),
    valid=({"tokens": 0}, {"tokens": 1500, "source": "montage", "limit_text": "лимит"}),
    invalid=({}, {"tokens": -1}, {"tokens": "много"}, {"tokens": True}),
    response_valid=({},),
)

_web_search = Message(
    "core.web_search", M2C, REQUEST, "Поиск в интернете средствами ядра.",
    request=(F("query", "string", True, "запрос", nonempty=True),),
    response=(F("text", "string", True, "найденное"),),
    errors=("bad_request", "unavailable", "internal"),
    valid=({"query": "погода"},), invalid=({}, {"query": ""}),
    response_valid=({"text": "..."},), response_invalid=({},),
)

# ── запросы к ядру, добавленные ТЗ 31 (версия контракта 1.1) ──────────────────

_llm_ask = Message(
    "core.llm.ask", M2C, REQUEST,
    "Один вызов локальной модели ядра (контракт `LanguageModel`). Срочный вызов вытесняет фоновый (`background: true`).",
    since="1.1",
    request=(F("system", "string", True, "системная часть промпта", max=LLM_TEXT_MAX),
             F("user", "string", True, "пользовательская часть", max=LLM_TEXT_MAX),
             F("timeout_s", "number", False, "тайм-аут вызова, секунд", min=1, max=LLM_TIMEOUT_MAX_S, default=60),
             F("max_tokens", "integer", False, "лимит токенов ответа", min=1, max=LLM_TOKENS_MAX, default=40),
             F("temperature", "number", False, "температура", min=0, max=2, default=0.3),
             F("background", "boolean", False, "фоновый вызов уступает срочным", default=False),
             F("image_b64", "string", False, "JPEG в base64 (до ~700 КБ)", max=LLM_IMAGE_B64_MAX),
             F("json", "boolean", False, "разобрать ответ как JSON и вернуть в data", default=False)),
    response=(F("text", "string", True, "ответ модели"), F("data", "object", False, "разобранный JSON (если просили и разобрался)")),
    errors=("bad_request", "unavailable", "timeout", "internal"),
    valid=({"system": "s", "user": "u"},
           {"system": "s", "user": "u", "timeout_s": 90, "max_tokens": 300, "temperature": 0.8, "background": True,
            "json": True}),
    invalid=({}, {"system": "s"}, {"system": "s", "user": "u", "max_tokens": 0}, {"system": "s", "user": "u", "temperature": 3},
             {"system": "s", "user": "u", "timeout_s": 1000}, {"system": 1, "user": "u"}),
    response_valid=({"text": "ок"}, {"text": "{}", "data": {}}),
    response_invalid=({}, {"text": 1}),
)

_events_tail = Message(
    "core.events.tail", M2C, REQUEST, "Последние события журнала ядра (только безопасные виды, см. LOG_READ_KINDS).",
    since="1.1",
    request=(F("limit", "integer", False, "сколько событий", min=1, max=TAIL_LIMIT_MAX, default=20),
             F("kinds", "array", False, "виды событий", items="string", max=len(LOG_READ_KINDS)),
             F("since", "number", False, "не раньше (секунды epoch)", min=0)),
    response=(F("events", "array", True, "события: ts, kind, text, data", items="object"),),
    errors=("bad_request", "unavailable"),
    valid=({}, {"limit": 8, "kinds": ["game"], "since": 1700000000}),
    invalid=({"limit": 0}, {"limit": 1000}, {"kinds": "game"}, {"since": "вчера"}),
    response_valid=({"events": []}, {"events": [{"ts": 1.0, "kind": "game", "text": "x"}]}),
    response_invalid=({}, {"events": "нет"}),
)

_claude_locate = Message(
    "core.claude.locate", M2C, REQUEST, "Путь к claude.exe для Claude Agent SDK (или null, если не найден).",
    since="1.1",
    request=(),
    response=(F("path", "string", False, "путь к claude.exe или отсутствует"),),
    errors=("unavailable",),
    valid=({},), invalid=("не объект",),
    response_valid=({"path": "C:/claude.exe"}, {}),
    response_invalid=({"path": 5},),
)

# ── сообщения модуля к ядру без ответа ────────────────────────────────────────

_hello = Message(
    "hello", M2C, NOTE, "Рукопожатие: первое сообщение модуля, с манифестом.",
    request=(F("module_id", "string", True, "id модуля"), F("instance", "string", True, "уникальный id запуска"),
             F("pid", "integer", True, "pid процесса"), F("protocol", "integer", True, "версия протокола"),
             F("manifest", "object", True, "манифест")),
    check=P.validate_hello,
    invalid=({}, "не объект", {"protocol": 99}),
    note="Ядро отвечает welcome или reject.",
)

_event = Message(
    "event", M2C, NOTE, "Событие модуля для менеджера внимания (ТЗ 03). Вид должен начинаться с `<id модуля>.`.",
    request=(F("kind", "string", True, "вид «<модуль>.<что>»"),
             F("text", "string", True, "что случилось", nonempty=True, max=P.EVENT_TEXT_MAX),
             F("data", "object", False, "подробности"),
             F("urgency_hint", "integer", False, "подсказка срочности", min=P.URGENCY_MIN, max=P.URGENCY_MAX),
             F("addressed_to_me", "boolean", False, "обращено к ассистентке"),
             F("memorable", "boolean", False, "достойно запоминания"),
             F("announce", "boolean", False, "объявление, звучит всегда"),
             F("sender", "object", False, "отправитель: id, name, is_owner"),
             F("reply_channel", "object", False, "куда отвечать: module, target"),
             F("dedupe_key", "string", False, "ключ склейки"),
             F("expires_in_s", "number", False, "срок жизни, секунд"),
             F("trace", "object", False, "трасса задержки (ТЗ 24)")),
    check=P.validate_event, max_bytes=P.MAX_MESSAGE_BYTES,
    valid=({"kind": "echo.said", "text": "привет"},
           {"kind": "telegram.message", "text": "пишут", "sender": {"id": "1", "name": "А", "is_owner": True},
            "reply_channel": {"module": "telegram", "target": "1"}, "urgency_hint": 2, "trace": {"id": "t1"}}),
    invalid=({}, {"kind": "Bad", "text": "x"}, {"kind": "a.b", "text": ""}, {"kind": "a.b", "text": "x" * 1001},
             {"kind": "a.b", "text": "x", "urgency_hint": 9}, {"kind": "a.b", "text": "x", "sender": {}}, "не объект"),
)

_say = Message(
    "say", M2C, NOTE, "Просьба сказать: режим fact (передать факт, формулирует модель) или verbatim (дословно).",
    request=(F("text", "string", True, "что сказать", nonempty=True), F("mode", "string", False, "режим", enum=P.SAY_MODES, default="fact"),
             F("emotion", "string", False, "эмоция"), F("key", "string", False, "ключ замены"),
             F("announce", "boolean", False, "объявление о долгом запуске"),
             F("urgency_hint", "integer", False, "подсказка срочности", min=P.URGENCY_MIN, max=P.URGENCY_MAX),
             F("expires_in_s", "number", False, "срок жизни, секунд")),
    check=P.validate_say,
    valid=({"text": "готово"}, {"text": "Готово", "mode": "verbatim", "emotion": "joy", "announce": True}),
    invalid=({}, {"text": " "}, {"text": "x", "mode": "loud"}, {"text": "x", "expires_in_s": 0}, "не объект"),
)

_emotion = Message(
    "emotion", M2C, NOTE, "Эмоция модуля (допустимость проверяет ядро).",
    request=(F("emotion", "string", True, "эмоция", nonempty=True), F("cause", "string", False, "причина")),
    valid=({"emotion": "joy", "cause": "победа"},), invalid=({}, {"emotion": 3}),
)

_state = Message(
    "state", M2C, NOTE, "Сводка состояния модуля для дашборда и выбора профиля.",
    request=(F("summary", "string", True, "сводка", max=P.STATE_SUMMARY_MAX), F("data", "object", False, "данные")),
    valid=({"summary": "работает"}, {"summary": "в игре", "data": {"colonists": 3}}),
    invalid=({}, {"summary": "x" * (P.STATE_SUMMARY_MAX + 1)}, {"summary": "x", "data": []}),
)

_job_update = Message(
    "job.update", M2C, NOTE, "Состояние долгой задачи модуля (ТЗ 01, раздел 9).",
    request=(F("job_id", "string", True, "id вида j-xxxxxxxx"), F("state", "string", True, "состояние", enum=P.JOB_STATES),
             F("title", "string", False, "заголовок"), F("stage", "string", False, "этап"), F("text", "string", False, "пояснение"),
             F("progress", "number", False, "прогресс", min=0, max=1),
             F("question", "object", False, "вопрос (обязателен в waiting_user)"),
             F("artifacts", "array", False, "результаты: объекты с path", items="object"),
             F("error", "object", False, "ошибка {code, message} (обязательна в failed)")),
    check=P.validate_job_update,
    valid=({"job_id": "j-0123abcd", "state": "running", "progress": 0.5},
           {"job_id": "j-0123abcd", "state": "failed", "error": {"code": "internal", "message": "сбой"}},
           {"job_id": "j-0123abcd", "state": "waiting_user",
            "question": {"question_id": "q1", "kind": "approval", "question": "Продолжить?"}}),
    invalid=({}, {"job_id": "x", "state": "running"}, {"job_id": "j-0123abcd", "state": "sleeping"},
             {"job_id": "j-0123abcd", "state": "running", "progress": 2},
             {"job_id": "j-0123abcd", "state": "waiting_user"}, {"job_id": "j-0123abcd", "state": "failed"}),
)

_panel_set = Message(
    "panel.set", M2C, NOTE, "Блоки пространства модуля в дашборде (ТЗ 15); не чаще раза в секунду, до 64 КиБ.",
    request=(F("blocks", "array", True, "блоки", items="object"),),
    check=lambda body, actions=None: P.validate_panel_set(body, actions), max_bytes=P.PANEL_SET_MAX_BYTES,
    valid=({"blocks": []}, {"blocks": [{"type": "text", "text": "привет"}]}),
    invalid=({}, {"blocks": "x"}, {"blocks": [{"type": "nope"}]}, [], "не объект"),
)

_log = Message(
    "log", M2C, NOTE, "Запись в журнал событий ядра (замена прямого `events_log`). Ядро принимает только виды LOG_KINDS "
    "и обрезает текст до 500 символов; выключенный модуль не пишет.",
    since="1.1",
    request=(F("kind", "string", True, "вид записи", enum=LOG_KINDS),
             F("text", "string", False, "текст", max=LOG_TEXT_MAX),
             F("data", "object", False, "подробности")),
    valid=({"kind": "error", "text": "сбой"}, {"kind": "telegram", "text": "подключён", "data": {"a": 1}}),
    invalid=({}, {"kind": "user", "text": "чужой вид"}, {"kind": "error", "text": 5}, {"kind": "error", "data": []}),
)

# ── ядро -> модуль ────────────────────────────────────────────────────────────

_c_tool_call = Message(
    "tool.call", C2M, REQUEST, "Вызов инструмента модуля из манифеста. `tool` без префикса модуля или с ним.",
    request=(F("tool", "string", True, "имя инструмента", nonempty=True), F("args", "object", False, "аргументы")),
    response=(), errors=("bad_request", "unknown", "unavailable", "denied", "internal", "timeout", "cancelled"),
    valid=({"tool": "repeat", "args": {"text": "а"}}, {"tool": "echo.repeat"}),
    invalid=({}, {"tool": ""}, {"tool": "x", "args": "нет"}),
    note="Результат: любое JSON-значение.",
)
_c_tool_check = Message(
    "tool.check", C2M, REQUEST, "Опасность вызова до выполнения (для инструментов с `danger_check: true`).",
    request=(F("tool", "string", True, "имя инструмента", nonempty=True), F("args", "object", False, "аргументы")),
    response=(F("danger", "string", False, "причина опасности или null"),), errors=("bad_request", "internal"),
    valid=({"tool": "run", "args": {"cmd": "ls"}},), invalid=({}, {"tool": 5}),
    response_valid=({"danger": None}, {"danger": "удаление"}), response_invalid=({"danger": 5},),
)
_c_job_cancel = Message(
    "job.cancel", C2M, REQUEST, "Отменить задачу модуля.",
    request=(F("job_id", "string", True, "id задачи", nonempty=True),), response=(), errors=("bad_request", "unknown"),
    valid=({"job_id": "j-0123abcd"},), invalid=({}, {"job_id": 1}),
)
_c_job_list = Message(
    "job.list", C2M, REQUEST, "Список задач модуля (после переподключения ядра).",
    request=(), response=(F("jobs", "array", True, "состояния задач", items="object"),), errors=("internal",),
    valid=({},), invalid=("не объект",), response_valid=({"jobs": []},), response_invalid=({},),
)
_c_graph_get = Message(
    "graph.get", C2M, REQUEST, "Подграф модуля фокуса для дашборда (манифест `graph: true`).",
    request=(), response=(F("nodes", "array", False, "узлы", items="object"), F("edges", "array", False, "связи", items="object")),
    errors=("unavailable", "internal"),
    valid=({},), invalid=("не объект",), response_valid=({"nodes": [], "edges": []}, {}), response_invalid=({"nodes": 3},),
)
_c_deliver = Message(
    "deliver", C2M, REQUEST, "Доставить ответ ассистентки в канал модуля (`capabilities: chat.channel`).",
    request=(F("target", "string", False, "адресат в канале (chat id и т.п.)"), F("text", "string", True, "текст", nonempty=True)),
    response=(F("delivered", "boolean", True, "доставлено ли"),), errors=("bad_request", "unavailable", "internal"),
    valid=({"target": "42", "text": "привет"}, {"text": "всем"}), invalid=({}, {"text": ""}, {"target": 1, "text": "x"}),
    response_valid=({"delivered": True},), response_invalid=({},),
)
_c_tts = Message(
    "tts.synthesize", C2M, REQUEST, "Синтез речи у модуля голоса (`capabilities: voice.output`), ответ на `core.tts`.",
    request=(F("text", "string", True, "текст", nonempty=True, max=TTS_TEXT_MAX), F("emotion", "string", False, "эмоция"),
             F("format", "string", False, "формат", enum=("wav", "ogg"), default="wav")),
    response=(F("path", "string", True, "путь к файлу"), F("duration_ms", "integer", True, "длительность, мс", min=0)),
    errors=("bad_request", "unavailable", "timeout", "internal"),
    valid=({"text": "привет"}, {"text": "привет", "emotion": None, "format": "ogg"}), invalid=({}, {"text": "x", "format": "mp3"}),
    response_valid=({"path": "a.wav", "duration_ms": 10},), response_invalid=({"path": "a.wav"},),
)
_c_panel_action = Message(
    "panel.action", C2M, REQUEST, "Нажатие кнопки или строки пространства модуля (ТЗ 15).",
    request=(F("action_id", "string", True, "id действия из panel.actions", nonempty=True), F("args", "object", False, "аргументы")),
    response=(F("ok", "boolean", True, "успех"), F("message", "string", False, "сообщение", max=P.PANEL_ACTION_RESULT_MESSAGE_MAX),
              F("open_url", "string", False, "что открыть")),
    errors=("bad_request", "unknown", "unavailable", "internal"),
    valid=({"action_id": "refresh"}, {"action_id": "go", "args": {"id": 1}}), invalid=({}, {"action_id": ""}),
    response_valid=({"ok": True}, {"ok": False, "message": "нельзя"}), response_invalid=({}, {"ok": "да"}),
)
_c_policy_activate = Message(
    "policy.activate", C2M, REQUEST, "Менеджер фокуса просит включить политику модуля (ТЗ 18).",
    request=(F("policy", "string", True, "имя политики", nonempty=True), F("goal", "string", False, "цель"),
             F("task_id", "string", False, "id задачи"), F("event_id", "string", False, "id события"),
             F("priority", "number", False, "приоритет"), F("reason", "string", False, "причина"),
             F("restored", "boolean", False, "возобновление"), F("checkpoint", "any", False, "сохранённое состояние"),
             F("subgoals", "array", False, "подцели")),
    response=(F("ok", "boolean", False, "принято"), F("policy", "string", False, "включённая политика")),
    errors=("bad_request", "unknown", "unavailable", "denied", "internal"),
    valid=({"policy": "working", "goal": "собрать урожай", "task_id": "t1"}, {"policy": "idle"}), invalid=({}, {"policy": ""}),
    response_valid=({"ok": True, "policy": "working"}, {}), response_invalid=({"ok": "да"},),
)
_c_focus_release = Message(
    "focus.release", C2M, REQUEST, "Менеджер фокуса просит освободить фокус и вернуть checkpoint (ТЗ 18).",
    request=(F("reason", "string", False, "причина"), F("task_id", "string", False, "id задачи"),
             F("force", "boolean", False, "принудительно"), F("event_id", "string", False, "id события"),
             F("checkpoint", "boolean", False, "нужен checkpoint")),
    response=(F("ok", "boolean", False, "освобождено"), F("checkpoint", "object", False, "состояние для возобновления"),
              F("error", "string", False, "причина отказа")),
    errors=("unknown", "unavailable", "internal"),
    valid=({"reason": "срочное", "task_id": "t1", "force": False, "checkpoint": True}, {}), invalid=({"force": "да"},),
    response_valid=({"ok": True, "checkpoint": {}}, {"ok": False, "error": "занята"}), response_invalid=({"checkpoint": 3},),
)
_c_focus_resume = Message(
    "focus.resume", C2M, REQUEST, "Менеджер фокуса возвращает задачу модулю с её checkpoint (ТЗ 18).",
    request=(F("task_id", "string", True, "id задачи", nonempty=True), F("policy", "string", False, "политика"),
             F("goal", "string", False, "цель"), F("checkpoint", "any", False, "сохранённое состояние"),
             F("subgoals", "array", False, "подцели")),
    response=(F("ok", "boolean", False, "возобновлено"), F("policy", "string", False, "политика")),
    errors=("unknown", "unavailable", "internal"),
    valid=({"task_id": "t1", "policy": "working", "goal": "g", "checkpoint": {}, "subgoals": []},), invalid=({}, {"task_id": ""}),
    response_valid=({"ok": True}, {}), response_invalid=({"ok": 1},),
)

# ── ядро -> модуль, без ответа ────────────────────────────────────────────────

_speak = Message(
    "speak", C2M, NOTE, "Произнести реплику (`capabilities: voice.output`).",
    request=(F("utterance_id", "string", True, "id реплики", nonempty=True), F("text", "string", True, "текст", nonempty=True),
             F("emotion", "string", False, "эмоция"), F("replace", "boolean", False, "заменить звучащую"),
             F("trace", "object", False, "трасса задержки")),
    valid=({"utterance_id": "u1", "text": "привет", "emotion": None, "replace": False},), invalid=({}, {"utterance_id": "u1"}),
)
_speak_stop = Message(
    "speak.stop", C2M, NOTE, "Замолчать: сразу или после предложения.",
    request=(F("utterance_id", "string", False, "какую реплику"), F("at", "string", False, "когда", enum=("now", "segment_end"))),
    valid=({"utterance_id": "u1", "at": "segment_end"}, {}), invalid=({"at": "потом"},),
)
_job_answer = Message(
    "job.answer", C2M, NOTE, "Ответ пользователя на вопрос задачи (`waiting_user`).",
    request=(F("job_id", "string", True, "id задачи", nonempty=True), F("question_id", "string", True, "id вопроса", nonempty=True),
             F("answer", "any", True, "ответ")),
    valid=({"job_id": "j-0123abcd", "question_id": "q1", "answer": True},), invalid=({}, {"job_id": "j-0123abcd"}),
)
_shutdown = Message(
    "shutdown", C2M, NOTE, "Ядро просит модуль завершиться (в течение 10 секунд).",
    request=(F("reason", "string", False, "причина"),), valid=({"reason": "выход"}, {}), invalid=({"reason": 5},),
)
_welcome = Message(
    "welcome", C2M, NOTE, "Ответ на hello: версия, включённость, настройки, профиль, батарейка.",
    request=(F("protocol", "integer", True, "версия протокола"), F("core_version", "string", False, "версия ядра"),
             F("enabled", "boolean", True, "включён ли модуль"), F("settings", "object", False, "общие настройки"),
             F("module_settings", "object", False, "настройки модуля"), F("profile", "object", False, "активный профиль"),
             F("battery", "object", False, "батарейка Claude")),
    valid=({"protocol": 1, "enabled": True, "settings": {}, "module_settings": {}, "profile": {}, "battery": {}},),
    invalid=({}, {"protocol": 1}),
)
_reject = Message(
    "reject", C2M, NOTE, "Отказ на hello (протокол или манифест неверны).",
    request=(F("reason", "string", True, "причина", nonempty=True),), valid=({"reason": "протокол"},), invalid=({},),
)

# ── темы ──────────────────────────────────────────────────────────────────────

_t_presence = Message(
    "presence", C2M, TOPIC, "Присутствие: говорит ли она, эмоция, занятость, фокус (не чаще 10 раз в секунду).",
    request=(F("speaking", "boolean", True, "говорит ли"), F("utterance_id", "string", False, "id звучащей реплики"),
             F("segment", "string", False, "звучащий фрагмент"), F("emotion", "string", True, "эмоция"),
             F("emotion_ts", "number", False, "когда сменилась эмоция"), F("busy", "boolean", False, "занята ли"),
             F("focus", "string", False, "что в фокусе"), F("user_speaking", "boolean", False, "говорит ли человек"),
             F("last_user_ts", "number", False, "когда человек писал или говорил"), F("last_say", "string", False, "последняя реплика"),
             F("last_say_ts", "number", False, "когда она сказала"))
    ,
    valid=({"speaking": False, "utterance_id": None, "segment": "", "emotion": "idle", "emotion_ts": 0.0, "busy": False,
            "focus": "idle", "user_speaking": False, "last_user_ts": 0.0, "last_say": "", "last_say_ts": 0.0},),
    invalid=({}, {"speaking": "да", "emotion": "idle"}, "не объект"),
)
_t_settings = Message(
    "settings", C2M, TOPIC, "Настройки: общие из `shared_settings` и собственные из `settings_schema` модуля.",
    request=(F("settings", "object", False, "общие настройки"), F("module_settings", "object", False, "настройки модуля")),
    valid=({"settings": {"dnd": False}, "module_settings": {"x": 1}}, {}), invalid=({"settings": []},),
)
_t_profile = Message(
    "profile", C2M, TOPIC, "Активный профиль: id, название, модули фокуса.",
    request=(F("id", "string", True, "id профиля", nonempty=True), F("title", "string", False, "название"),
             F("focus_modules", "array", False, "модули фокуса", items="string")),
    valid=({"id": "assistant", "title": "Ассистент", "focus_modules": []},), invalid=({}, {"id": "a", "focus_modules": "x"}),
)
_t_battery = Message(
    "battery", C2M, TOPIC, "Батарейка Claude: процент и сон.",
    request=(F("percent", "integer", True, "процент", min=0, max=100), F("sleeping", "boolean", True, "спит ли")),
    valid=({"percent": 80, "sleeping": False},), invalid=({}, {"percent": 120, "sleeping": False}, {"percent": 1}),
)
_t_character = Message(
    "character", C2M, TOPIC, "Карточка активного персонажа при смене (ТЗ 05, CHR-6).",
    request=(F("id", "string", True, "id", nonempty=True), F("name", "string", True, "имя", nonempty=True),
             F("aliases", "array", False, "другие имена", items="string"), F("gender", "string", False, "род"),
             F("language", "string", False, "язык"), F("world", "string", False, "мир"), F("voice", "object", False, "голос")),
    valid=({"id": "glados", "name": "ГЛаДОС", "aliases": [], "gender": "f", "language": "ru", "world": "", "voice": {}},),
    invalid=({}, {"id": "glados"}),
)
_t_module = Message(
    "module", C2M, TOPIC, "Включение и выключение модуля (MOD-14). Клиент подписан всегда.",
    request=(F("enabled", "boolean", True, "включён ли"),), valid=({"enabled": False},), invalid=({}, {"enabled": "нет"}),
)

CORE_REQUEST_MESSAGES = (_ask, _tts, _tool_call, _lookup, _recall, _remember, _exp_add, _kb_ingest, _persona, _privacy,
                         _claude_usage, _web_search, _llm_ask, _events_tail, _claude_locate)
MODULE_NOTE_MESSAGES = (_hello, _event, _say, _emotion, _state, _job_update, _panel_set, _log)
MODULE_REQUEST_MESSAGES = (_c_tool_call, _c_tool_check, _c_job_cancel, _c_job_list, _c_graph_get, _c_deliver, _c_tts,
                           _c_panel_action, _c_policy_activate, _c_focus_release, _c_focus_resume)
CORE_NOTE_MESSAGES = (_welcome, _reject, _job_answer, _speak, _speak_stop, _shutdown)
TOPIC_MESSAGES = (_t_presence, _t_settings, _t_profile, _t_battery, _t_character, _t_module)

# `core.tts` и `tts.synthesize`, `tool.call` и `core.tool.call` носят разные имена, поэтому ключ реестра - (направление, имя)
ALL_MESSAGES = (CORE_REQUEST_MESSAGES + MODULE_NOTE_MESSAGES + MODULE_REQUEST_MESSAGES + CORE_NOTE_MESSAGES + TOPIC_MESSAGES)
