"""Тексты страницы docs/architecture-clean.html: сущности, проблемы кода, соответствие файлов, шаги рефакторинга."""

ENT = [
    ("Event", "Любой стимул: реплика, событие игры, сообщение, срок напоминания, мысль. Поля: вид, текст, источник (внешний или self), отправитель, подсказка срочности, данные, метка трассы.", "сейчас четыре объекта: словарь шины, <code>Item</code>, <code>UserInput</code>, часть <code>SpeechCandidate</code>"),
    ("Decision", "Приоритет 0–100, уровень D0–D4, маршруты (say, chat, task, focus, think, digest, ignore), причина и уверенность.", "поля <code>Item</code> и журнал; отдельной сущности нет"),
    ("Impulse (self.*)", "Внутренний стимул: срок напоминания, которое агент назначил себе, или мысль, что-то напомнившая. Становится обычным Event и проходит тот же конвейер.", "нет"),
    ("Thought / Intent", "Thought: запись внутреннего диалога (не озвучивается). Intent: вывод размышления: сказать, сделать, напомнить себе, записать, ничего.", "нет"),
    ("SpeechCandidate / Utterance", "Что и с каким приоритетом хочет сказать агент; очередь выбирает момент, канал доставляет.", "<code>speech_queue.SpeechCandidate</code>"),
    ("Plan / ToolCall / ToolResult", "Шаги из инструментов, вызов инструмента, результат или код ошибки.", "словари в <code>router.py</code>"),
    ("Tool", "Инструмент: имя, схема параметров, риск, видимость по профилю, исполнитель.", "<code>environments/base.Tool</code>"),
    ("Slot / FocusDecision / Checkpoint", "Единственный слот фокуса, очередь событий, состояние прерванной задачи.", "<code>focus.py</code>"),
    ("MemoryRecord / MemoryHit", "Единица памяти: факт, запись о человеке, воспоминание о себе, план, (план) эпизод, урок, убеждение; результат поиска с близостью.", "четыре формата: граф, <code>MemoryStore</code>, отношения, диалог"),
    ("Person · Character · Job", "Человек или колонист; данные персонажа; долгая задача модуля.", "<code>relationships.py</code> · <code>character.py</code> · <code>module_registry.py</code>"),
]
SMELLS = [
    ("Бог-объекты", "<code>Attention</code>: 1108 строк, 70 методов. <code>Companion</code>: 37 методов, 15 импортов. <code>FocusManager</code>: 60 методов.", "разрезать по сценариям (шаги R3–R5)"),
    ("Слои знают друг друга", "<code>attention</code> читает <code>companion._rel</code>, <code>_mem</code>, <code>graph</code> через <code>getattr</code> и зовёт <code>companion._speak_about</code>; <code>module_hub</code> больше не импортирует <code>attention</code> и <code>fast_brain</code> (приёмник <code>EventSink</code>, ТЗ 29 R6).", "контракты и колбэки"),
    ("Глобальное состояние", "<code>events_log</code> (16 импортов), <code>character</code> (16), <code>settings.load()</code> (14), блокировка модели в <code>fast_brain</code>.", "передавать через конструкторы"),
    ("Корень сборки с логикой (устранено в R6)", "обработчики voice, colonists, run_lost, game_order вынесены в <code>app/handlers/</code>; <code>core_service.py</code> только собирает.", "только сборка"),
    ("Нет единой сущности события", "Словарь, <code>Item</code>, <code>UserInput</code>; правила «замолчи» и «похоже на просьбу» ещё и в <code>router</code>.", "<code>Event</code> и один модуль правил"),
    ("Интерфейс ходит в данные (частично устранено в R7)", "<code>dashboard/</code>: роутеры и презентеры; настройки читаются через <code>SettingsProvider</code>, остальное через объекты ядра в <code>ctx</code>.", "контракты чтения"),
    ("Проактивность по таймеру", "Было: <code>Companion.proactive_loop</code> раз в 30 с спрашивал модель, стоит ли заговорить. Удалено вместе с <code>app/proactive_legacy.py</code> и настройкой <code>proactive_tick</code>.", "заменено внутренними импульсами и размышлением (ТЗ 30)"),
    ("ContextBuilder: бюджет есть, поиск ещё два", "<code>app/context_builder.py</code>: бюджет токенов на роль и режимы push/pull есть (настройка <code>context</code>), по умолчанию прежняя склейка; общий гибридный поиск <code>search/</code> готов, но чтение памяти ещё идёт двумя поисками.", "подключить search/ к MemoryReader"),
]
MAP = [
    ("attention.py (1108)", "Normalizer, PeopleDirectory, HardRules, PriorityScorer, Dispatcher, Digest, JobTracker (часть), вопросы"),
    ("companion.py (666)", "ConversationService, ContextBuilder, ReplyGenerator, MemoryWriter (capture, consolidate); проактивность по таймеру удалена (ТЗ 30)"),
    ("focus.py (1061)", "FocusPolicy (чистая), FocusManager (очередь и переходы), CheckpointStore"),
    ("router.py (225) + approvals.py (301)", "Planner, RiskPolicy, Approver, Executor"),
    ("core_service.py", "Composition root; обработчики в <code>app/handlers/</code>"),
    ("module_hub.py + module_outbound.py", "ModuleHub: входящие, исходящие в <code>ModuleHub.outbound</code>, без импорта домена"),
    ("dashboard/", "HTTP-адаптер: роутеры <code>routes_*.py</code> и презентеры <code>presenters.py</code>"),
    ("graph_memory.py, memory_store.py, user_memory.py", "MemoryWriter/MemoryReader + репозитории"),
    ("speech_queue.py (296)", "SpeechQueue: остаётся как есть"),
    ("events_log.py, settings.py, character.py", "контракты EventLog, SettingsProvider, CharacterRepo"),
    ("нет", "ReminderStore, ImpulseTimer, ReflectionService, журнал мыслей (ТЗ 30)"),
]
PHASES = [
    ("R0", "Страховка", "Тест архитектуры по импортам; текущие тесты без изменений."),
    ("R1", "Домен", "Чистые функции в <code>agent/domain/</code>: оценка и D0–D4, правила и «замолчи», дедупликация, риск, политика фокуса."),
    ("R2", "Контракты", "<code>agent/contracts.py</code> (Protocol) и адаптеры поверх существующих модулей."),
    ("R3", "Разрезать Attention", "ProcessEvent (Normalizer → HardRules → Classifier → Scorer → Dispatcher), SpeechCoordinator, Digest, QuestionService, JobTracker; фасад <code>Attention</code> остаётся."),
    ("R4", "Разрезать Companion", "ConversationService, ContextBuilder, ReplyGenerator, MemoryCapture; проактивность по таймеру выделена временно и удалена в ТЗ 30."),
    ("R5", "Разрезать FocusManager", "FocusPolicy, FocusQueue, CheckpointStore, FocusDispatch."),
    ("R6", "Сборка и транспорт", "<code>core_service</code> только собирает; <code>module_hub</code> без импорта домена."),
    ("R7", "Дашборд", "Роутеры и презентеры, чтение через контракты."),
    ("ТЗ 30", "Внутренние импульсы и размышление", "ReminderStore, ImpulseTimer, ReflectionService, PlanNudger; <code>proactive_loop</code> убран."),
    ("ТЗ 32", "Workspace и общие задачи", "WorkspaceManager, TaskService, Planner, TaskStore: задачи, напоминания себе и планы пользователя живут в <code>tasks.db</code>."),
]
