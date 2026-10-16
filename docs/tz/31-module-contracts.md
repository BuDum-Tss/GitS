# 31. Контракты для модулей: всё, чем модуль может пользоваться, описано явно

Этап 5, после ТЗ 29 (шаги R2 и R6). Репозиторий: `GitS`, папки `sdk/` и `agent/`. Ветка: `tz/31-module-contracts`. Читать: `docs/tz/01-module-contract.md`, `docs/MODULES.md`, `sdk/gits_sdk/protocol.py`, `agent/core_requests.py`, `docs/tz/29-clean-architecture.md`.
Решение владельца (2026-10-15): всё, что модули могут использовать, должно иметь чёткие контракты.

## 1. Цель
Модуль зависит только от публичного контракта (SDK): типизированные запросы и ответы, темы, события, состояние, задачи, ресурсы, переменные окружения. Никаких импортов внутренностей ядра. Контракт один, версионируется, проверяется одним и тем же кодом на стороне ядра, SDK и тестов.

## 2. Исходное состояние (измерено 2026-10-15)
- Хорошо описано: рукопожатие и манифест, `event`, `say`, `job.update`, `ask`, панель (`validate_*` в `protocol.py`).
- Запросы модуля к ядру (`core.ask`, `core.tts`, `core.tool.call`, `core.persona`, `core.privacy_terms`, `core.claude_usage`, `core.web_search`) проверяются **внутри** `agent/core_requests.py` (схемы вшиты в обработчики), а не в общем протоколе; клиент отправляет их универсальным `request(msg_type, dict)` и получает неописанный словарь. Пять запросов (`core.lookup`, `core.memory.recall`, `core.memory.remember`, `core.experience.add`, `core.kb.ingest`) возвращают `unavailable`: контракт без реализации.
- Содержимое тем (`presence`, `settings`, `profile`, `battery`, `character`) описано в документах, но не схемами.
- Модули **импортируют внутренности ядра**, минуя SDK: `events_log` (voice, telegram, desktop, rimworld: 13 обращений), `fast_brain` (voice, telegram, desktop), `safety` и `claude_cli` (desktop). В ветке `tz/24` добавляется `latency_trace`.
- Переменные окружения (`GITS_*`, `CHARACTER_*`) и папки ресурсов (`gits_sdk.resources`) описаны частично.

## 3. Требования
- **MC-1 (ОБЯЗАТЕЛЬНО). Единый источник контракта.** Пакет `sdk/gits_sdk/contract/`: типизированные описания (`dataclass` или `TypedDict`) для каждого запроса модуля к ядру и ответа (`CoreAsk`, `CoreTts`, `CoreToolCall`, `CorePersona`, `CorePrivacyTerms`, `CoreClaudeUsage`, `CoreWebSearch`, `CoreLookup`, `CoreMemoryRecall`, `CoreMemoryRemember`, `CoreExperienceAdd`, `CoreKbIngest`), для запросов ядра к модулю (`tool.call`, `tool.check`, `job.cancel`, `job.list`, `graph.get`, `deliver`, `tts.synthesize`, `panel.action`, `policy.activate`, `focus.release`, `focus.resume`), для тем (`presence`, `settings`, `profile`, `battery`, `character`), событий, состояния, задач, ошибок (коды MOD-9). У каждого: имя, направление, поля и типы, обязательность, ограничения размера, коды ошибок, статус (`stable`, `planned`, `deprecated`), версия появления.
- **MC-2 (ОБЯЗАТЕЛЬНО). Проверка в одном месте.** `validate_*` в SDK строятся из этих описаний; `agent/core_requests.py` и `module_hub` вызывают их вместо вшитых проверок. Клиент SDK проверяет исходящее и (в режиме разработки) входящее.
- **MC-3 (ОБЯЗАТЕЛЬНО). Типизированный клиент.** `ModuleClient` получает методы по контракту вместо универсального `request`: `tts(text, ...)`, `ask(...)`, `call_tool(...)`, `persona()`, `memory_recall(...)`, `memory_remember(...)` и т. д. с типизированными результатами; `request(...)` остаётся для совместимости.
- **MC-4 (ОБЯЗАТЕЛЬНО). Модули не импортируют внутренности ядра.** Всё, что им нужно, предоставляет SDK или ядро запросом:
  журнал событий (`events_log`) → запрос или метод клиента `log(kind, text, **data)`;
  локальная модель (`fast_brain`) → запрос `core.llm.ask` по контракту `LanguageModel` (ТЗ 29, R2) с лимитами и учётом очереди «срочное вытесняет фоновое»;
  `safety.danger_of` → вспомогательная функция в SDK (`gits_sdk.safety`) или `tool.check` ядра;
  `claude_cli` (путь к Claude) → `core.claude.locate` или SDK-функция;
  `latency_trace` (ТЗ 24) → SDK (`gits_sdk.trace`).
  Тест в `agent/tests/test_architecture.py`: код модулей импортирует только стандартную библиотеку, сторонние пакеты, `gits_sdk` и свои файлы. Исключения перечислены списком с датой удаления.
- **MC-5 (ОБЯЗАТЕЛЬНО). Реализовать или пометить.** Пять запросов памяти и справочника: либо реализация поверх контрактов `MemoryReader`/`MemoryWriter` (ТЗ 29), либо статус `planned` в контракте и понятная ошибка `unavailable` с кодом и версией.
- **MC-6 (ОБЯЗАТЕЛЬНО). Окружение и ресурсы.** Таблица переменных окружения, которые передаёт супервизор и читает модуль (`GITS_MODULE_TOKEN`, `GITS_CORE_URL`, `GITS_MODULE_ID`, `GITS_CHARACTER_ID`, `MEMORY_DIR`, `GITS_RESOURCES_DIR`…), и правила папки ресурсов `resources/<Имя>/<модуль>/` в контракте с тестом «супервизор действительно их передаёт».
- **MC-7 (ОБЯЗАТЕЛЬНО). Набор проверок на соответствие.** `sdk/tests/test_contract_conformance.py`: одни и те же примеры (валидные и невалидные) для каждого элемента контракта прогоняются через проверки SDK, через ядро (`module_hub` с настоящим `core_requests`) и через `FakeCore`; все три обязаны отвечать одинаково. `FakeCore` строится из контракта, а не отдельно.
- **MC-8 (ОБЯЗАТЕЛЬНО). Версии и совместимость.** Версия протокола (`PROTOCOL`) и правила: добавление необязательных полей и новых запросов не ломает; удаление и смена типа поднимают версию; список устаревшего с датой. Тест: старый манифест и старые тела сообщений (зафиксированные примеры) проходят проверку текущей версии.
- **MC-9 (ОБЯЗАТЕЛЬНО). Документ из кода.** Скрипт `sdk/tools/gen_contract_doc.py` строит `docs/MODULE_CONTRACT.md` (таблицы и JSON-схемы в `docs/contract/*.json`) из описаний; тест сверяет, что файлы актуальны. `docs/MODULES.md` ссылается на сгенерированное.
- **MC-10 (ЖЕЛАТЕЛЬНО).** Пример модуля `sdk/examples/echo_module.py` обновить под типизированный клиент; шаблон нового модуля (`sdk/template/`).

## 4. Порядок
После ТЗ 29 R2 (внутренние контракты `agent/contracts.py` задают `LanguageModel` и память) и до или вместе с R6 (корень сборки и транспорт). Разница: `agent/contracts.py` внутренние (между сценариями ядра и адаптерами), `gits_sdk/contract` публичные и версионируемые (между ядром и модулями).

## 5. Приёмка
- Все 12 запросов модуля к ядру и 11 запросов ядра к модулю имеют описание, проверку из одного места и пример в тесте соответствия.
- Поиск по `modules/` не находит импортов модулей ядра (кроме списка исключений); тест это ловит.
- `docs/MODULE_CONTRACT.md` создан скриптом и совпадает с кодом.
- Существующие модули (voice, telegram, pet, desktop, rimworld, montage) работают как раньше; `.\check.ps1` зелёный.

## 6. Статус (выполнено)
MC-1…MC-9 выполнены: `sdk/gits_sdk/contract/`, типизированный клиент, перевод модулей telegram, voice, desktop, pet, montage на SDK (rimworld и montage ядерных импортов не имели; rimworld оставлен на `client.request`, чтобы не менять его заглушки), тесты соответствия, совместимости и окружения, генератор `sdk/tools/gen_contract_doc.py`. Пять запросов памяти и справочника получили статус `planned` (ответ `unavailable` с версией контракта 1.1). MC-10 выполнен (ветка `chore/leftovers`): шаблон модуля `sdk/template/` (манифест, обработчики на типизированном клиенте, тесты на `FakeCore`, идут в `check.ps1`), `echo_module.py` указывает на шаблон и типизированные методы клиента (набор его инструментов не менялся). Запросы `core.memory.recall` и `core.memory.remember` реализованы поверх `MemoryReader.search` и `MemoryWriter.add_fact` (статус `stable`, версия контракта 1.2); остальные три (`core.lookup`, `core.experience.add`, `core.kb.ingest`) остаются `planned`: для них в ядре пока нет базы знаний.
