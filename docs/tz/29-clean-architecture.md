# 29. Чистая архитектура ядра: домен, сценарии, контракты, адаптеры

Этап 5, между ТЗ 24 и ТЗ 25 (фаза 1b плана `docs/ROADMAP.md`). Репозиторий: `GitS`, папка `agent/`. Ветки: `tz/29-rN-<кратко>` от `master`, по одной на шаг R0–R7. Читать: `docs/architecture-clean.html` (целевая диаграмма компонентов, редактируемая копия `docs/architecture-clean.drawio`), `docs/ROADMAP.md`, `AGENTS.md`, `docs/tz/25-fast-contour.md`.

## 1. Цель
Разрезать три бог-объекта и корень сборки так, чтобы каждое решение агента (приём события, правила, оценка, маршрут, ответ, действие, фокус, память) было отдельным небольшим компонентом с понятным входом и выходом, а зависимости шли внутрь. Поведение агента **не меняется**: это перенос и разрезание, а не переписывание. Рефакторинг нужен, чтобы ТЗ 25 (маршруты, стриминг, дедлайны), 27 (эпизоды, сон) и 28 (уроки) ложились на готовые места.

## 2. Исходное состояние (измерено 2026-10-15 разбором исходников `agent/*.py`)
- 46 модулей, 12 054 строки.
- `attention.py`: 1108 строк, класс `Attention` с 70 методами (приём события, правила R1–R4, классификатор, оценка, решение D0–D4, сводка, вопросы, задачи, речь, стек фокуса).
- `companion.py`: 666 строк, 1 класс, 37 методов, 15 импортов (разговор, сборка контекста, консолидация памяти, проактивность, вывод речи, ответ на разрешения, эскалация к Claude).
- `focus.py`: 1061 строка, `FocusManager` с 60 методами.
- `core_service.py`: 22 импорта; внутри обработчики voice, colonists, run_lost, game_order.
- `module_hub.py` импортирует `attention` и `fast_brain`; `attention` читает приватные поля `companion._rel`, `_mem`, `graph` через `getattr` и зовёт `companion._speak_about`.
- Глобальные зависимости: `events_log` (16 импортов), `character` (16), `settings.load()` (14), блокировка модели в `fast_brain`.
- `dashboard.py`: 863 строки, 58 функций, 0 классов, прямой доступ к хранилищам памяти.
- Правила «замолчи» и «похоже на просьбу» дублируются в `attention` и `router`.
Если при выполнении цифры разошлись с кодом, не придумывай замену: опиши расхождение и спроси человека.

Целевая схема и порядок раскрытия деталей: `docs/architecture-clean.html` (сначала основа из десяти блоков, затем слои).

## 3. Целевая структура
```
agent/
  domain/    entities.py scoring.py rules.py risk.py focus_policy.py salience.py   (без ввода-вывода)
  app/       process_event.py conversation.py context_builder.py planner.py executor.py focus_manager.py
             memory_capture.py proactive_legacy.py digest.py jobs.py                               (сценарии; reflection.py и impulses.py придут по ТЗ 30)
  contracts.py   Classifier, LanguageModel, MemoryReader, MemoryWriter, EventLog, SpeechOutput, ToolInvoker, Approver, Clock, SettingsProvider
  adapters/  module_hub.py dashboard/ llm_lmstudio.py classifier_gliner.py graph_graphiti.py stores_json.py
             event_log_sqlite.py speech_voice_module.py supervisor.py
  core_service.py   только сборка
```
Правило зависимостей: `domain` не импортирует ничего из `app`, `adapters` и внешних пакетов ввода-вывода; `app` зависит от `domain` и `contracts`; `adapters` зависят от `contracts` и `domain`; `core_service` знает всё.

## 4. Требования
Каждый шаг R1–R7 это отдельная ветка и отдельный коммит-набор; после каждого `.\check.ps1` зелёный и существующие тесты не правятся (кроме импортов), новые тесты добавляются на новые компоненты.

- **ARC-1 (ОБЯЗАТЕЛЬНО, шаг R0). Страховка.** `agent/tests/test_architecture.py`: по `ast` проверяет, что `agent/domain/*` не импортирует `app`, `adapters`, `settings`, `events_log`, `fast_brain`, `aiohttp`, `httpx`, `sqlite3`; изначально отчёт, после R1 жёсткая проверка. Измеряет размер классов (предупреждение выше 400 строк и 40 методов).
- **ARC-2 (ОБЯЗАТЕЛЬНО, R1). Домен без ввода-вывода.** В `agent/domain/` переезжают чистые функции: оценка приоритета и решение D0–D4 (`Attention._score`, `_decide`, веса), правила R1–R4 и «замолчи» (единое место, `router` использует его же), дедупликация (`_merge_duplicate` без хранения), политика риска (`router._step`, часть про `needs_ask`), политика фокуса (`_gate`, `_near`, `_claimant`, `_conflict`, старение), яркость (`salience`, если ТЗ 24 влито). Исходные классы вызывают новые функции; результаты в тестах те же.
- **ARC-3 (ОБЯЗАТЕЛЬНО, R2). Контракты.** `agent/contracts.py` с `Protocol`: `Classifier`, `LanguageModel` (fast и slow), `MemoryReader`, `MemoryWriter`, `EventLog`, `SpeechOutput`, `ToolInvoker`, `Approver`, `Clock`, `SettingsProvider`. Существующие модули оборачиваются адаптерами (`decide.Decider`, `fast_brain`, `graph_memory`, `events_log`, `speech_out`); сценарии получают контракты через конструктор. Глобальные `settings.load()`, `events_log.log` в сценариях заменяются контрактами по мере переноса.
- **ARC-4 (ОБЯЗАТЕЛЬНО, R3). Разрезать `Attention`.** `app/process_event.py` (Normalizer → HardRules → Classifier → Scorer → Dispatcher, по ТЗ 25 FAST-1 это и есть `routes.py`), `SpeechCoordinator` (клей с очередью речи), `app/digest.py`, `QuestionService`, `JobTracker`. Класс `Attention` остаётся тонким фасадом с прежними публичными методами (`submit`, `submit_speech`, `submit_question`, `on_job_update`, `enqueue_say`…). Доступ к профилю человека и памяти идёт через контракты `PeopleDirectory` и `MemoryWriter`, а не через `getattr(companion, "_rel")`.
- **ARC-5 (ОБЯЗАТЕЛЬНО, R4). Разрезать `Companion`.** `ConversationService`, `ContextBuilder` (явный компонент, сначала повторяет нынешнюю склейку шести источников), `ReplyGenerator`, `MemoryCapture` (`_learn`, `_consolidate`, `remember_text`). `proactive_loop` (опрос себя раз в 30 с) выделяется в отдельный временный сценарий `proactive_legacy.py` **без смены поведения**: по решению владельца агент не должен говорить по таймеру, его заменяют внутренние импульсы и размышление (ТЗ 30). Фасад `Companion` сохраняет публичный интерфейс (`on_voice`, `on_user_message`, `say`, `proactive_loop`…).
- **ARC-6 (ОБЯЗАТЕЛЬНО, R5). Разрезать `FocusManager`. Сделано (ветка tz/29-r5-focus): `domain/focus_policy.py` (`gate`, `ambiguity`, `needs_recheck`, `holder_priority`), `focus_queue.py`, `focus_store.py`, `FocusDispatch` в `focus_dispatch.py`; `focus.py` фасад с прежними публичными методами.** `FocusPolicy` (чистая), `FocusQueue` (состояния событий, склейка, старение), `CheckpointStore`, `FocusDispatch` (контракт к модели). Протокол переключения и формат `focus.json` не меняются.
- **ARC-7 (ОБЯЗАТЕЛЬНО, R6). Корень сборки и транспорт.** `core_service.py` только создаёт объекты и подставляет контракты; обработчики voice, colonists, run_lost, game_order переезжают в `app/handlers`; `module_hub.py` получает приёмник событий и запросов колбэком (контракт `EventSink`), не импортирует `attention` и `fast_brain`; исходящие команды модулям выделяются в `ModuleHub.outbound`.
- **ARC-8 (ЖЕЛАТЕЛЬНО, R7). Дашборд.** Роутеры по темам и презентеры; чтение данных только через контракты.
- **ARC-9 (ОБЯЗАТЕЛЬНО). Документы.** После каждого шага обновляются `AGENTS.md` (карта модулей), `docs/architecture-clean.html` и `.drawio` (статус блоков: выделен или нет), `CHANGELOG.md`.

## 4a. Выполнено
- **R2 (ветка `tz/29-r2-contracts`):** `agent/contracts.py` (Protocol: модель, классификатор, журнал, настройки, память чтение и запись, люди, граф, часы, разрешения, инструменты) и `agent/adapters/` (обёртки над старыми модулями). `Companion`, `Router`, `Attention` принимают контракты необязательными параметрами; без них работают адаптеры, `core_service.py` не менялся. Оставлено прямым (объяснение в CHANGELOG и отчёте): `fast_brain.react_chat`, `publish`, `_json_of`, `EMOTIONS`; `character.active()`; `settings.DEFAULTS` и `settings.memory_dir()` в `attention.py`; `companion._speak_about` в `environments/builtin.py` и `_repeats_recent` в `attention.py` (заглушки тестов его не имеют в публичном виде); `claude_quota`, `gamenet`, `prompts`. Контракт `SpeechOutput` не введён (шагу не нужен, вывод речи уже подставляется через `companion.speech`); `Attention` память на чтение не использует.
- **R0, R1 (ветка `tz/29-r0-r1`, влита):** тест архитектуры; `agent/domain/` с `scoring`, `rules`, `risk`, `focus_policy`. Уточнения по факту: `speech_queue.py` это асинхронный координатор (задачи и futures), поэтому в домен он **не переносится** (ARC-1 запрещает `asyncio` в домене), а станет сценарием слоя `app` с чистой политикой выбора, выделенной в домен позже; правила «замолчи» и «похоже на просьбу» это два разных правила, объединять их нельзя (изменит поведение), оба лежат в `domain/rules.py`; `_merge_duplicate` (дедупликация) и яркость переносятся в R3.

## 5. Порядок и зависимости
R0 → R1 → R2 → R3 → R4 → R5 → R6 → R7. R3 готовит почву для ТЗ 25 (маршруты и быстрый контур) и ТЗ 30 (маршрут `think`), R4 — для ТЗ 25 (стриминг ответа в `ReplyGenerator`) и ТЗ 27, R5 независим и может идти параллельно R4. ТЗ 24 (трасса) желательно влить до R3: метки времени проходят через новые компоненты.

## 6. Приёмка
- Все существующие тесты ядра, SDK и модулей зелёные без правок логики (допустимы правки импортов в тестах).
- `test_architecture.py` проходит в жёстком режиме: домен чист, контракты не импортируют адаптеры, `module_hub` не импортирует домен.
- Размеры: ни один класс в `app/` больше 400 строк; `core_service.py` меньше 200 строк и меньше 15 импортов.
- На живом ядре (ручной прогон по просьбе владельца) голосовой ответ, событие игры, просьба с разрешением и переключение фокуса работают как раньше (сверка по журналу событий и трассе ТЗ 24).
