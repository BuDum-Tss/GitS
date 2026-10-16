// Создано tools/architecture/build.py из tools/architecture/spec.py. Не править вручную.
window.ARCH_DATA = {
 "groups": [
  {
   "id": "g_src",
   "row": 0,
   "title": "Источники событий",
   "tag": "I",
   "new": false,
   "summary": "Модули (голос, Telegram, экран, игра, монтаж) и чат дашборда. Присылают события и выполняют команды.",
   "out": "Event",
   "nodes": [
    {
     "id": "voice",
     "t": "voice",
     "tag": "I",
     "new": false,
     "do": "микрофон, VAD, распознавание речи (WhisperX), узнавание голоса; озвучка (Fish Speech)",
     "out": "Event voice.utterance",
     "now": "modules/voice"
    },
    {
     "id": "desk",
     "t": "desktop",
     "tag": "I",
     "new": false,
     "do": "экран, активность, мышь и клавиатура, Claude-помощник",
     "out": "Event desktop.*, job.update",
     "now": "modules/desktop"
    },
    {
     "id": "tg",
     "t": "telegram",
     "tag": "I",
     "new": false,
     "do": "бот, её аккаунт, посты канала",
     "out": "Event telegram.*",
     "now": "modules/telegram"
    },
    {
     "id": "rim",
     "t": "rimworld",
     "tag": "I",
     "new": false,
     "do": "игровой агент: ходы, комментатор, запись забега",
     "out": "Event raid, death, colonists",
     "now": "modules/rimworld"
    },
    {
     "id": "mont",
     "t": "montage",
     "tag": "I",
     "new": false,
     "do": "конвейер роликов: Remotion + ffmpeg, агент Claude",
     "out": "job.update, вопросы",
     "now": "modules/montage"
    },
    {
     "id": "dash",
     "t": "Дашборд и чат",
     "tag": "I",
     "new": false,
     "do": "чат, настройки, пульт, оценки",
     "out": "UserInput как Event",
     "now": "dashboard.py: 863 строки, 0 классов, ходит в хранилища напрямую"
    }
   ]
  },
  {
   "id": "g_imp",
   "row": 0,
   "title": "Внутренние импульсы",
   "tag": "A",
   "new": false,
   "summary": "Напоминания, которые агент назначил себе; сработавший срок становится событием. Болтовни по таймеру нет.",
   "out": "Event self.*",
   "nodes": [
    {
     "id": "reminders",
     "t": "ReminderStore",
     "tag": "I",
     "new": false,
     "do": "напоминания, назначенные самим агентом: срок, текст, откуда пришла мысль",
     "out": "срок наступил",
     "now": "есть (ТЗ 30): adapters/impulse_store.py (JsonReminders, файл reminders.json), контракт ReminderRepository; инструмент assistant.remind_self"
    },
    {
     "id": "timer",
     "t": "ImpulseTimer",
     "tag": "I",
     "new": false,
     "do": "следит только за сроками напоминаний; когда срок пришёл, шлёт событие. Не тик болтовни",
     "out": "Event self.reminder",
     "now": "есть (ТЗ 30): app/impulse_timer.py, один таймер на ближайший срок, восстановление и склейка просроченного; proactive_loop выключен настройкой proactive_tick"
    }
   ]
  },
  {
   "id": "g_in",
   "row": 1,
   "title": "Приём",
   "tag": "I",
   "new": false,
   "summary": "Принимает событие из любого источника, проверяет, приводит к единому виду, склеивает повторы, узнаёт отправителя.",
   "out": "Event",
   "nodes": [
    {
     "id": "hub",
     "t": "ModuleHub (входящие)",
     "tag": "I",
     "new": false,
     "do": "WebSocket :8766: рукопожатие, проверка формата, очередь входящих; запросы core.*",
     "out": "Event",
     "now": "module_hub.py: 683 строки; импортирует attention и fast_brain, то есть транспорт знает домен"
    },
    {
     "id": "event",
     "t": "Event",
     "tag": "D",
     "new": false,
     "do": "единый конверт: kind, text, sender, hint, data, trace, ts; источник внешний или внутренний (self)",
     "out": "сущность",
     "now": "частично (ТЗ 30): domain/event.py (Event с origin external/self) для импульсов и размышления; внутри внимания остаётся Item (поле origin)"
    },
    {
     "id": "norm",
     "t": "Normalizer",
     "tag": "A",
     "new": false,
     "do": "нормализация, дедупликация за 60 с, склейка повторов, TTL",
     "out": "Event",
     "now": "выделен (ТЗ 29, R3): app/normalizer.py, app/items.py (хранилище и склейка), domain/dedupe.py"
    },
    {
     "id": "people",
     "t": "PeopleDirectory",
     "tag": "A",
     "new": false,
     "do": "sender → Person, первый контакт, заметки о человеке",
     "out": "Person",
     "now": "выделен (R3): Normalizer.note_sender через контракты PeopleDirectory и MemoryWriter; getattr(companion, \"_rel\") остался только в адаптере adapters/people.py"
    }
   ]
  },
  {
   "id": "g_dec",
   "row": 2,
   "title": "Решение",
   "tag": "A",
   "new": false,
   "summary": "Правила до моделей, быстрая классификация, оценка важности и выбор маршрута: сказать, ответить, сделать, сменить фокус, подумать, отложить.",
   "out": "Decision + routes",
   "nodes": [
    {
     "id": "rules",
     "t": "HardRules",
     "tag": "D",
     "new": false,
     "do": "до моделей: R1 владелец → разговор, R2 ответ на вопрос, R3 mute, R4 критичное, «замолчи»",
     "out": "RuleResult",
     "now": "выделен (R3): app/process_event.py HardRules; чистые правила «замолчи» и «похоже на просьбу» в domain/rules.py"
    },
    {
     "id": "cls",
     "t": "Classifier (контракт)",
     "tag": "A",
     "new": false,
     "do": "категория, срочность, адресат, нужен ли фокус; реализация: служба GLiNER :8768; тайм-аут 1 с",
     "out": "Classification",
     "now": "decide.py + services/decider; в Attention его зовёт app/event_classifier.py (R3)"
    },
    {
     "id": "score",
     "t": "PriorityScorer",
     "tag": "D",
     "new": false,
     "do": "срочность × 20 × веса (профиль, отправитель, правила, пользователь) × свежесть",
     "out": "priority 0..100",
     "now": "выделен (R3): app/scorer.py поверх domain/scoring.py"
    },
    {
     "id": "disp",
     "t": "Dispatcher",
     "tag": "A",
     "new": false,
     "do": "решение D0–D4 и routes[]: say, chat, task, focus, think, digest, ignore; пишет решение в журнал",
     "out": "Decision",
     "now": "выделен (R3): app/dispatcher.py (finish, act) и app/decision_log.py; routes[] пока только say, digest, focus-хук"
    }
   ]
  },
  {
   "id": "g_conv",
   "row": 3,
   "title": "Разговор",
   "tag": "A",
   "new": false,
   "summary": "Ведёт беседу: собирает контекст из памяти, пишет ответ быстрой моделью.",
   "out": "ReplyCandidate",
   "nodes": [
    {
     "id": "conv",
     "t": "ConversationService",
     "tag": "A",
     "new": false,
     "do": "обращение владельца: проверки → ветка (болтовня / просьба / «что умеешь») → ответ",
     "out": "Reply или Task",
     "now": "app/conversation_service.py (фасад Companion)"
    },
    {
     "id": "ctx",
     "t": "ContextBuilder",
     "tag": "A",
     "new": false,
     "do": "собирает контекст: характер, память, диалог, состояние; бюджет токенов на роль",
     "out": "Context",
     "now": "app/context_builder.py: прежняя склейка 6 источников, бюджета токенов пока нет"
    },
    {
     "id": "reply",
     "t": "ReplyGenerator",
     "tag": "A",
     "new": false,
     "do": "LanguageModel(fast) → LM Studio (Gemma): ответ; страховки стиля",
     "out": "ReplyCandidate",
     "now": "app/reply_generator.py: _converse, _declutter + fast_brain"
    }
   ]
  },
  {
   "id": "g_think",
   "row": 3,
   "title": "Размышление",
   "tag": "A",
   "new": false,
   "summary": "Внутренний диалог агента: получает событие или импульс, думает, читая память, и решает: сказать, сделать, напомнить себе или записать.",
   "out": "Intent",
   "nodes": [
    {
     "id": "reflect",
     "t": "ReflectionService",
     "tag": "A",
     "new": false,
     "do": "внутренний диалог в несколько шагов по событию или импульсу; читает память; лимиты шагов и токенов",
     "out": "Intent: сказать, сделать, напомнить, записать, ничего",
     "now": "есть (ТЗ 30): app/reflection.py; маршрут think из Dispatcher (domain/rules.think_route); лимиты шагов, токенов, часа, глубины"
    },
    {
     "id": "thoughts",
     "t": "Журнал мыслей",
     "tag": "I",
     "new": false,
     "do": "внутренние рассуждения: не озвучиваются; основа дневника и эпизодов",
     "out": "Thought",
     "now": "есть (ТЗ 30): adapters/impulse_store.py (JsonThoughts, thoughts.json), GET /api/thoughts"
    }
   ]
  },
  {
   "id": "g_act",
   "row": 3,
   "title": "Дела",
   "tag": "A",
   "new": false,
   "summary": "Строит план из доступных инструментов, спрашивает разрешение на опасное, выполняет в модулях, ведёт долгие задачи.",
   "out": "ToolResult",
   "nodes": [
    {
     "id": "reg",
     "t": "ToolRegistry",
     "tag": "A",
     "new": false,
     "do": "инструменты ядра и модулей, видимость по профилю, отбор по смыслу",
     "out": "Tool[]",
     "now": "environments/registry.py, tool_search.py"
    },
    {
     "id": "plan",
     "t": "Planner",
     "tag": "A",
     "new": false,
     "do": "LanguageModel → LM Studio (Gemma): план шагов или «не могу»",
     "out": "Plan",
     "now": "router._plan"
    },
    {
     "id": "risk",
     "t": "RiskPolicy + Approver",
     "tag": "D",
     "new": false,
     "do": "риск auto / ask / public / danger; вопрос пользователю, ответ да/нет",
     "out": "Approval",
     "now": "router._step + approvals.py: 301 строка"
    },
    {
     "id": "exec",
     "t": "Executor (ToolInvoker)",
     "tag": "A",
     "new": false,
     "do": "вызов инструмента в ядре или модуле, коды ошибок, итог-факт",
     "out": "ToolResult",
     "now": "router._step и _run_step"
    },
    {
     "id": "jobs",
     "t": "JobTracker",
     "tag": "A",
     "new": false,
     "do": "долгие задачи: прогресс, вопросы, итог; Claude-помощник (Agent SDK)",
     "out": "JobState",
     "now": "выделен (R3) для внимания: app/jobs.py (JobTracker); module_registry и desktop.assistant_agent как раньше"
    }
   ]
  },
  {
   "id": "g_foc",
   "row": 3,
   "title": "Фокус",
   "tag": "A",
   "new": false,
   "summary": "Решает, кому отдать главную роль (например, игре в бою), и когда её вернуть.",
   "out": "FocusDecision",
   "nodes": [
    {
     "id": "fmgr",
     "t": "FocusManager (фасад)",
     "tag": "A",
     "new": false,
     "do": "один слот, протокол переключения, команды пользователя, журнал; прежние публичные методы",
     "out": "FocusDecision",
     "now": "focus.py: фасад над четырьмя частями (ТЗ 29, ARC-6)"
    },
    {
     "id": "fpol",
     "t": "FocusPolicy",
     "tag": "D",
     "new": false,
     "do": "чистые правила: приоритет, запас, критичное, старение, конфликты",
     "out": "FocusDecision",
     "now": "domain/focus_policy.py: gate, ambiguity, дребезг, эшелон"
    },
    {
     "id": "fque",
     "t": "FocusQueue",
     "tag": "A",
     "new": true,
     "do": "состояния событий, склейка, старение, сроки",
     "out": "QueuedEvent",
     "now": "focus_queue.py"
    },
    {
     "id": "fstore",
     "t": "CheckpointStore",
     "tag": "A",
     "new": true,
     "do": "checkpoint задач и файл focus.json",
     "out": "checkpoint",
     "now": "focus_store.py"
    },
    {
     "id": "fdisp",
     "t": "FocusDispatch (контракт)",
     "tag": "A",
     "new": false,
     "do": "Gemma только для неоднозначного; ответ строго проверяется",
     "out": "Op",
     "now": "focus_dispatch.py: класс FocusDispatch и validate"
    }
   ]
  },
  {
   "id": "g_out",
   "row": 4,
   "title": "Вывод",
   "tag": "I",
   "new": false,
   "summary": "Очередь речи выбирает момент и приоритет, затем реплика уходит каналом: голос, чат, Telegram, питомец. Команды модулям уходят отсюда же.",
   "out": "Utterance, команды",
   "nodes": [
    {
     "id": "queue",
     "t": "SpeechQueue",
     "tag": "A",
     "new": false,
     "do": "приоритеты, условия «не перебиваю», интервалы, прерывание, TTL; чистую политику выбора выделить в домен позже",
     "out": "Utterance",
     "now": "speech_queue.py: 296 строк, асинхронный координатор (asyncio-задачи), в домен целиком не переносится"
    },
    {
     "id": "digest",
     "t": "Digest",
     "tag": "A",
     "new": false,
     "do": "сводка событий D1 и предложение рассказать",
     "out": "SpeechCandidate",
     "now": "выделен (R3): app/digest.py"
    },
    {
     "id": "out",
     "t": "SpeechOutput и каналы (контракты)",
     "tag": "I",
     "new": false,
     "do": "голос (speak), чат, Telegram (deliver), питомец",
     "out": "доставка",
     "now": "speech_out.py, conversation_io.py"
    },
    {
     "id": "aff",
     "t": "Presence и эмоции",
     "tag": "A",
     "new": false,
     "do": "говорит ли, эмоция, присутствие; (план) настроение",
     "out": "PresenceState",
     "now": "PresenceTracker + fast_brain.publish"
    },
    {
     "id": "hubout",
     "t": "ModuleHub (исходящие)",
     "tag": "I",
     "new": false,
     "do": "доставка команд модулям: tool.call, speak, policy.activate, job.answer",
     "out": "ToolResult, события",
     "now": "тот же module_hub.py, смешан с приёмом"
    }
   ]
  },
  {
   "id": "g_mem",
   "row": 5,
   "title": "Память",
   "tag": "A",
   "new": false,
   "summary": "Запись и чтение памяти: диалог, факты, профили, воспоминания о себе. Источник правды — журнал событий.",
   "out": "MemoryRecord, MemoryHit",
   "nodes": [
    {
     "id": "mw",
     "t": "MemoryWriter",
     "tag": "A",
     "new": false,
     "do": "сразу: диалог и журнал; в паузе: факты (Gemma + эмбеддинги); раз в 6 реплик: осмысление; (план) эпизоды и сон",
     "out": "MemoryRecord",
     "now": "app/memory_capture.py: _learn, _consolidate, remember_text + graph_memory.step"
    },
    {
     "id": "mr",
     "t": "MemoryReader",
     "tag": "A",
     "new": false,
     "do": "граф + воспоминания о себе + записи; (план) гибридный поиск",
     "out": "MemoryHit[]",
     "now": "graph.search, self_memories, MemoryStore.relevant (два поиска)"
    },
    {
     "id": "elog",
     "t": "EventLog",
     "tag": "I",
     "new": false,
     "do": "SQLite events.db: журнал событий, трасса, решения",
     "out": "контракт журнала",
     "now": "events_log.py: глобальные функции, 16 импортов"
    },
    {
     "id": "stores",
     "t": "Диалог, записи, люди",
     "tag": "I",
     "new": false,
     "do": "JSON: диалог, MemoryStore (планы, напоминания), отношения",
     "out": "репозитории",
     "now": "user_memory.py, memory_store.py, relationships.py"
    },
    {
     "id": "graph",
     "t": "FactGraph (контракт)",
     "tag": "I",
     "new": false,
     "do": "Graphiti: факты с интервалами валидности",
     "out": "контракт графа",
     "now": "graph_memory.py: 701 строка"
    },
    {
     "id": "char",
     "t": "CharacterRepo",
     "tag": "I",
     "new": false,
     "do": "resources/<Имя>: конституция, prompt, канон, голос",
     "out": "Character",
     "now": "character.py + resources/"
    }
   ]
  },
  {
   "id": "g_ext",
   "row": 5,
   "title": "Внешний слой и сборка",
   "tag": "X",
   "new": false,
   "summary": "Модели и службы: LM Studio, GLiNER, FalkorDB, Fish Speech, WhisperX, Claude. Сборка: core_service.",
   "out": "",
   "nodes": [
    {
     "id": "lms",
     "t": "LM Studio :1234",
     "tag": "X",
     "new": false,
     "do": "Gemma (речь, план, факты) и EmbeddingGemma",
     "out": "",
     "now": ""
    },
    {
     "id": "gli",
     "t": "GLiNER decider :8768",
     "tag": "X",
     "new": false,
     "do": "классификатор событий и обращений",
     "out": "",
     "now": ""
    },
    {
     "id": "falk",
     "t": "FalkorDB :6380",
     "tag": "X",
     "new": false,
     "do": "база графа фактов",
     "out": "",
     "now": ""
    },
    {
     "id": "fish",
     "t": "Fish Speech / edge-tts",
     "tag": "X",
     "new": false,
     "do": "синтез речи",
     "out": "",
     "now": ""
    },
    {
     "id": "wx",
     "t": "WhisperX / Whisper",
     "tag": "X",
     "new": false,
     "do": "распознавание речи",
     "out": "",
     "now": ""
    },
    {
     "id": "cl",
     "t": "Claude Agent SDK",
     "tag": "X",
     "new": false,
     "do": "долгие и сложные задачи",
     "out": "",
     "now": ""
    },
    {
     "id": "sup",
     "t": "Supervisor",
     "tag": "I",
     "new": false,
     "do": "запуск и перезапуск процессов модулей",
     "out": "",
     "now": "supervisor.py: 573 строки"
    },
    {
     "id": "root",
     "t": "Composition root",
     "tag": "I",
     "new": false,
     "do": "только собирает объекты и подставляет контракты; бизнес-логики быть не должно",
     "out": "",
     "now": "core_service.py: 22 импорта, внутри обработчики voice, colonists, run_lost"
    }
   ]
  }
 ],
 "edges": [
  [
   "voice",
   "hub",
   "flow",
   "Event"
  ],
  [
   "desk",
   "hub",
   "flow",
   null
  ],
  [
   "tg",
   "hub",
   "flow",
   null
  ],
  [
   "rim",
   "hub",
   "flow",
   null
  ],
  [
   "mont",
   "hub",
   "flow",
   "job.update"
  ],
  [
   "dash",
   "hub",
   "flow",
   "UserInput"
  ],
  [
   "reminders",
   "timer",
   "use",
   "срок"
  ],
  [
   "timer",
   "norm",
   "flow",
   "self.reminder"
  ],
  [
   "hub",
   "norm",
   "flow",
   "Event"
  ],
  [
   "norm",
   "people",
   "use",
   "sender"
  ],
  [
   "norm",
   "rules",
   "flow",
   "Event"
  ],
  [
   "rules",
   "cls",
   "flow",
   "не решено правилами"
  ],
  [
   "cls",
   "score",
   "flow",
   "Classification"
  ],
  [
   "score",
   "disp",
   "flow",
   "priority"
  ],
  [
   "rules",
   "conv",
   "flow",
   "R1: владелец"
  ],
  [
   "disp",
   "conv",
   "flow",
   "route chat"
  ],
  [
   "disp",
   "plan",
   "flow",
   "route task"
  ],
  [
   "disp",
   "fmgr",
   "flow",
   "route focus"
  ],
  [
   "disp",
   "reflect",
   "flow",
   "route think"
  ],
  [
   "disp",
   "queue",
   "flow",
   "route say: факт"
  ],
  [
   "disp",
   "digest",
   "flow",
   "D1"
  ],
  [
   "conv",
   "ctx",
   "use",
   null
  ],
  [
   "ctx",
   "mr",
   "r",
   "запрос"
  ],
  [
   "ctx",
   "reply",
   "use",
   "Context"
  ],
  [
   "reply",
   "queue",
   "flow",
   "ReplyCandidate"
  ],
  [
   "conv",
   "plan",
   "flow",
   "просьба"
  ],
  [
   "reflect",
   "mr",
   "r",
   "вспоминает"
  ],
  [
   "reflect",
   "thoughts",
   "w",
   "мысль"
  ],
  [
   "reflect",
   "queue",
   "flow",
   "хочет сказать"
  ],
  [
   "reflect",
   "plan",
   "flow",
   "хочет сделать"
  ],
  [
   "reflect",
   "reminders",
   "w",
   "напомнить себе"
  ],
  [
   "reflect",
   "mw",
   "w",
   "записать"
  ],
  [
   "reg",
   "plan",
   "use",
   "Tool[]"
  ],
  [
   "plan",
   "risk",
   "flow",
   "Plan"
  ],
  [
   "risk",
   "exec",
   "flow",
   "разрешённый шаг"
  ],
  [
   "exec",
   "hubout",
   "flow",
   "tool.call"
  ],
  [
   "exec",
   "jobs",
   "use",
   null
  ],
  [
   "jobs",
   "queue",
   "flow",
   "итог"
  ],
  [
   "exec",
   "queue",
   "flow",
   "итог-факт"
  ],
  [
   "fmgr",
   "fpol",
   "use",
   "правила"
  ],
  [
   "fmgr",
   "fdisp",
   "use",
   "неоднозначно"
  ],
  [
   "fmgr",
   "fque",
   "use",
   "события"
  ],
  [
   "fmgr",
   "fstore",
   "use",
   "checkpoint"
  ],
  [
   "fmgr",
   "hubout",
   "ctl",
   "policy.activate"
  ],
  [
   "queue",
   "out",
   "flow",
   "Utterance"
  ],
  [
   "out",
   "aff",
   "use",
   null
  ],
  [
   "out",
   "hubout",
   "flow",
   "speak"
  ],
  [
   "digest",
   "queue",
   "flow",
   null
  ],
  [
   "conv",
   "mw",
   "w",
   "после ответа"
  ],
  [
   "disp",
   "elog",
   "w",
   "Decision"
  ],
  [
   "mw",
   "graph",
   "w",
   null
  ],
  [
   "mw",
   "stores",
   "w",
   null
  ],
  [
   "mr",
   "graph",
   "r",
   null
  ],
  [
   "mr",
   "stores",
   "r",
   null
  ],
  [
   "ctx",
   "char",
   "r",
   "характер"
  ],
  [
   "graph",
   "falk",
   "ext",
   null
  ]
 ],
 "macroLabels": [
  [
   "g_dec",
   "g_conv",
   "ответить"
  ],
  [
   "g_dec",
   "g_act",
   "сделать"
  ],
  [
   "g_dec",
   "g_foc",
   "сменить фокус"
  ],
  [
   "g_dec",
   "g_think",
   "подумать"
  ],
  [
   "g_dec",
   "g_out",
   "просто сказать"
  ],
  [
   "g_conv",
   "g_out",
   "реплика"
  ],
  [
   "g_conv",
   "g_act",
   "просьба"
  ],
  [
   "g_think",
   "g_out",
   "хочет сказать"
  ],
  [
   "g_think",
   "g_act",
   "хочет сделать"
  ],
  [
   "g_act",
   "g_out",
   "итог дела"
  ],
  [
   "g_foc",
   "g_out",
   "режим модулю"
  ],
  [
   "g_conv",
   "g_mem",
   "читает и пишет"
  ],
  [
   "g_think",
   "g_mem",
   "читает и пишет"
  ],
  [
   "g_think",
   "g_imp",
   "напомнить себе"
  ],
  [
   "g_src",
   "g_in",
   "Event"
  ],
  [
   "g_imp",
   "g_in",
   "Event self.*"
  ],
  [
   "g_in",
   "g_dec",
   "Event"
  ],
  [
   "g_dec",
   "g_mem",
   "журнал решений"
  ],
  [
   "g_act",
   "g_src",
   "команды"
  ]
 ],
 "layers": {
  "D": [
   "Домен",
   "#7a5ac8",
   "#efe9fb"
  ],
  "A": [
   "Сценарии",
   "#2b6fba",
   "#e6f0fb"
  ],
  "I": [
   "Адаптеры",
   "#1f9d55",
   "#e3f5ea"
  ],
  "X": [
   "Внешнее",
   "#6b7683",
   "#eceff3"
  ]
 },
 "edgeKinds": {
  "flow": [
   "поток события и данных",
   "#7a5ac8",
   false
  ],
  "use": [
   "вызов сценария, контракта",
   "#2b6fba",
   false
  ],
  "w": [
   "запись в память",
   "#1f9d55",
   false
  ],
  "r": [
   "чтение из памяти",
   "#c2308a",
   false
  ],
  "ext": [
   "обращение к внешней службе",
   "#d9822b",
   false
  ],
  "ctl": [
   "команда, управление",
   "#c2412d",
   true
  ]
 }
};
