# 02. Ядро: шина модулей, супервизор, профили, разговор, маршрутизатор, дашборд

Этап 1. Репозиторий: `GitS`. Ветка: `tz/02-core`.
Читать перед этим: `00-architecture.md`, `01-module-contract.md`. ТЗ 03 (внимание) делается после этого и опирается на
интерфейсы, заданные здесь.

## 1. Цель

Превратить процесс ядра GitS в «мозг с разъёмами»:
- принимает модули по контракту 01 и ведёт их реестр;
- сам запускает и перезапускает процессы модулей (супервизор), трей запускает только ядро;
- профили из файлов вместо `modes.py` + `profiles.py`;
- инструменты модулей попадают в маршрутизатор с учётом `availability` и активного профиля;
- разговор принимает обращения из любого канала и отвечает в тот же канал;
- речь уходит через единый интерфейс вывода (сейчас — старый путь через питомца, после ТЗ 12 — модуль `voice`);
- дашборд показывает модули, их задачи и настройки.

Поведение ассистентки для пользователя после этого ТЗ не меняется: всё, что работало, работает (раздел 13).

## 2. Исходное состояние (проверь перед началом)

- `agent/main.py` → `core_service._run(config)`: создаёт компаньона, зрение, слух, микрофон, Telegram, реестр
  окружений, маршрутизатор, дашборд (`dashboard.start_dashboard`, порт 8766).
- `agent/modes.py`: `PROFILES`, `ModeManager` (авто: сон по батарейке, RimWorld по запущенной игре, иначе ассистент;
  `set_override`; `module_active`; `graph()` для `/api/modes`).
- `agent/profiles.py`: тон по активному окну (`game`, `work`, `stream`, `general`), `hint()` добавляется к характеру.
- `agent/environments/registry.py`: `Registry` с `gate` (модуль окружения включён в режиме), `search` по основам слов.
- `agent/router.py`: `Router.handle(text)` — планировщик на Gemma, `run_tool` для агентов игр.
- `agent/dashboard.py`: эндпоинты (список в разделе 11), `_SPEECH` с `say`, `status`, `event` для агента игры.
- `agent/companion.py`: `say()` пишет `say.json` и журнал; озвучку играет процесс питомца (`desktop_pet.py` →
  `tts_engine.speak_sequence`); `is_speaking()` оценивается по длине текста.
- `agent/tray.py`: запускает ядро, питомца и агента игры (`RIMWORLD_AGENT_DIR`).

## 3. Объём

Входит: шина модулей, супервизор, реестр модулей, профили, инструменты модулей в маршрутизаторе, каналы ввода и
ответа разговора, интерфейс вывода речи, вопросы модулей пользователю (`core.ask`), запросы модулей к ядру из ТЗ 01
(кроме тех, что относятся к памяти и справочнику — заглушки до ТЗ 04), дашборд, совместимость со старыми HTTP-каналами
игры.

Не входит: менеджер внимания (ТЗ 03; здесь — простая заглушка-«пропускатель», раздел 9.4), перенос Telegram, голоса,
питомца, рабочего стола и RimWorld в модули (ТЗ 10–14), память и справочник (ТЗ 04).

## 4. Новые и изменённые файлы

| Файл | Что |
|---|---|
| `agent/module_hub.py` | новый: WebSocket-сервер `/module/ws`, рукопожатие, соединения, запросы-ответы, темы |
| `agent/module_registry.py` | новый: реестр модулей (манифест, состояние, последний `state`, задачи), окружения из манифестов |
| `agent/supervisor.py` | новый: запуск и перезапуск процессов модулей по `agent/modules.yaml` |
| `agent/modules.yaml` | новый: какие модули есть и как их запускать |
| `agent/profiles/` + `agent/profile_manager.py` | новые: профили в YAML и выбор активного; заменяют `modes.py` и `profiles.py` |
| `agent/conversation_io.py` | новый: каналы ввода и ответа разговора |
| `agent/speech_out.py` | новый: интерфейс вывода речи и реализация «старый путь через питомца» |
| `agent/attention.py` | новый: заглушка менеджера внимания (полная реализация — ТЗ 03) |
| `agent/core_service.py`, `router.py`, `environments/registry.py`, `dashboard.py`, `companion.py`, `approvals.py`, `settings.py`, `tray.py` | изменения |
| `agent/modes.py`, `agent/profiles.py` | удалить после переноса (импорты заменить) |
| `GitS/sdk` | зависимость окружения ядра (`pip install -e ./sdk` в README и `requirements.txt` как `-e ./sdk` — проверь, что `check.ps1` и установка работают из корня репозитория) |

## 5. Шина модулей (`module_hub.py`)

- **CORE-1 (ОБЯЗАТЕЛЬНО).** Маршрут `GET /module/ws` добавляется в то же aiohttp-приложение, что и дашборд (порт 8766).
  Проверки MOD-3 (петля) и MOD-4 (токен) до апгрейда до WebSocket. Токен генерируется при старте ядра и пишется в
  `%LOCALAPPDATA%\GLaDOS\module_token` (атомарно: временный файл + `os.replace`).
- **CORE-2 (ОБЯЗАТЕЛЬНО).** Рукопожатие по ТЗ 01 §5: ждать `hello` 10 с (иначе закрыть), проверить `protocol == 1`,
  манифест (`glados_module.protocol.validate_manifest`), единственность `voice.output` (MOD-18), вытеснение
  дубликата (MOD-13). Ответ `welcome` с текущими настройками (`shared_settings` модуля), `module_settings`, профилем,
  батарейкой, `enabled`.
- **CORE-3 (ОБЯЗАТЕЛЬНО).** Класс `ModuleConnection` с методами:
  `async request(type, body, timeout) -> value` (бросает `ModuleError(code, message)`),
  `async send(type, body)`, `async close(code, reason)`. Ожидающие запросы при обрыве завершаются ошибкой
  `unavailable`.
- **CORE-4 (ОБЯЗАТЕЛЬНО).** Приём сообщений модуля: каждый тип из ТЗ 01 §7 диспетчеризуется в обработчик
  (`event` → `attention.submit`, `say` → `attention.submit_speech`, `emotion` → `fast_brain.publish`, `state` →
  реестр, `job.update` → реестр + `attention`, `core.*` → обработчики раздела 10). Обработчики запросов выполняются
  отдельными задачами asyncio; ответ `result` всегда отправляется (успех или ошибка).
- **CORE-5 (ОБЯЗАТЕЛЬНО).** Темы: `hub.broadcast(topic, data)` шлёт `topic` всем подключённым и включённым модулям,
  подписанным на тему. `presence` — не чаще 10 раз в секунду (последнее значение побеждает), остальные — сразу.
- **CORE-6 (ОБЯЗАТЕЛЬНО).** Журнал: подключение, отключение (с причиной), `reject` (с причиной) пишутся в
  `events_log` (вид `module`) и в журнал ядра. Содержимое сообщений в журнал не пишется (только тип и размер).

## 6. Реестр модулей (`module_registry.py`)

- **CORE-7 (ОБЯЗАТЕЛЬНО).** Запись о модуле: `id`, `manifest`, `connected: bool`, `instance`, `pid`, `version`,
  `connected_at`, `last_disconnect_reason`, `enabled` (из настроек), `state` (последнее `state.summary` и `data`),
  `jobs: dict[job_id, JobStatus]` (последние `job.update`), `restarts` (из супервизора). Запись живёт и при отключении
  (модуль «офлайн»).
- **CORE-8 (ОБЯЗАТЕЛЬНО).** Окружение из манифеста: для каждого модуля в реестр окружений (`environments.registry`)
  добавляется `Environment(id=<module_id>, title, description, module=<module_id>)` с инструментами из манифеста.
  `Tool.call` для них = `hub.connection(module_id).request("tool.call", {"tool": name, "args": args},
  timeout=tool.timeout_s)`. Для инструментов с `danger_check` синхронный `Tool.danger` не годится, поэтому
  маршрутизатор перед вызовом такого инструмента делает `await` запроса `tool.check` (CORE-35). Ошибка `ModuleError` превращается
  в `ToolError` с сообщением модуля.
- **CORE-9 (ОБЯЗАТЕЛЬНО).** Состояние окружения модуля (`Environment.status`): `(False, "модуль не подключён")`,
  `(False, "модуль выключен")` или `(True, "доступно")`. При переподключении с другим манифестом окружение
  пересобирается.
- **CORE-10 (ОБЯЗАТЕЛЬНО).** Видимость инструмента маршрутизатору (замена `Registry.gate`): инструмент виден, если
  окружение доступно И (`availability == "always"` ИЛИ `module_id` в `focus_modules` активного профиля). Встроенные
  окружения ядра (`environments/builtin.py`) получают `availability = "always"` у всех инструментов, кроме окружений
  с `module != "core"` (их видимость — по профилю, как сейчас). В `Tool` (`environments/base.py`) добавить поля
  `availability: str = "always"`, `timeout_s: int = 150`, `job: bool = False`, `danger_check: bool = False`.
- **CORE-11 (ОБЯЗАТЕЛЬНО).** `Registry.describe()` и `snapshot()` учитывают новую видимость; формулировка для
  невидимых: «не в фокусе текущего профиля».

## 7. Супервизор (`supervisor.py`, `modules.yaml`)

Формат `agent/modules.yaml`:

```yaml
# Модули, которые ядро запускает само. Пути: абсолютные или относительно папки agent/.
# {GITS}, {PYTHON} и переменные окружения в фигурных скобках подставляются.
modules:
  - id: telegram
    cwd: "{GITS}/modules/telegram"
    command: "{PYTHON}"
    args: ["-u", "main.py"]
    autostart: true          # запускать, если модуль включён в настройках
    restart: on-failure      # always | on-failure | never
  - id: rimworld
    cwd: "{RIMWORLD_MODULE_DIR}/agent"
    command: "{RIMWORLD_MODULE_DIR}/.venv/Scripts/python.exe"   # если нет — {PYTHON}
    args: ["-u", "main.py"]
    autostart: true
    restart: on-failure
```

- **CORE-12 (ОБЯЗАТЕЛЬНО).** `{GITS}` — корень репозитория GitS, `{PYTHON}` — `sys.executable` ядра. Переменные
  `{ИМЯ}` — из окружения процесса ядра (после загрузки `secrets.env`); если переменной нет, модуль помечается
  «не настроен» с причиной и не запускается. Если `command` не существует как файл и это не `{PYTHON}` — запасной
  `{PYTHON}` и предупреждение в журнал.
- **CORE-13 (ОБЯЗАТЕЛЬНО).** Запуск: `asyncio.create_subprocess_exec` без окна консоли (`CREATE_NO_WINDOW`), `cwd`,
  окружение = окружение ядра + `GLADOS_MODULE_TOKEN`, `GLADOS_CORE_URL`, `GLADOS_MODULE_ID`, `PYTHONUNBUFFERED=1`.
  stdout и stderr — в `agent/logs/modules/<id>.log` (ротация: 5 МБ × 3 файла; каталог в `.gitignore`).
- **CORE-14 (ОБЯЗАТЕЛЬНО).** Модуль запускается, если в настройках включён (`settings["modules"][id]["enabled"]`,
  по умолчанию `true` для всех из `modules.yaml`) и `autostart: true`. Выключение в дашборде: ядро шлёт модулю
  `shutdown`, ждёт 10 с выхода процесса, затем убивает дерево процессов (`psutil`, как `tray._kill_tree`).
- **CORE-15 (ОБЯЗАТЕЛЬНО).** Перезапуск по политике `restart`: задержки 2, 5, 15, 60 с; если процесс падает 5 раз за
  10 минут — супервизор перестаёт перезапускать, помечает модуль «остановлен после сбоев» с последними 20 строками
  stderr (без строк, похожих на токены: длинные hex и `Bearer`), событие в `events_log`. Кнопка «перезапустить» в
  дашборде сбрасывает счётчик.
- **CORE-16 (ОБЯЗАТЕЛЬНО).** Модуль, запущенный вручную (не супервизором), принимается как обычный; супервизор не
  запускает второй экземпляр, пока этот подключён.
- **CORE-17 (ОБЯЗАТЕЛЬНО).** При остановке ядра супервизор шлёт `shutdown` всем и через 10 с убивает оставшиеся
  процессы, которые запускал сам.
- **CORE-18 (ОБЯЗАТЕЛЬНО).** `tray.py`: запускает только ядро и питомца (питомца — до ТЗ 13); пункт меню агента игры
  удаляется после ТЗ 11, а до этого остаётся как есть. Включение модулей — в дашборде.

На этапе 1 `modules.yaml` содержит только эхо-модуль, выключенный по умолчанию (`enabled: false` в DEFAULTS для
`echo`). Реальные модули добавляют ТЗ 10–14 и 11.

## 8. Профили (`agent/profiles/*.yaml`, `profile_manager.py`)

Формат файла профиля:

```yaml
id: rimworld
title: RimWorld
description: Игровой агент: мод, контуры хода, планирования и тактики.
tone: >
  Сейчас пользователь играет, а ты играешь вместе с ним: коротко, по делу, с азартом. Не отвлекай длинными
  речами, говори о происходящем в колонии.
focus_modules: [rimworld]          # инструменты availability=profile этих модулей видны
memory_scopes: [rimworld]          # к global всегда; используется в ТЗ 04
knowledge: [rimworld.wiki, rimworld.defs, rimworld.experience]   # ТЗ 04
attention:                         # ТЗ 03; здесь только хранится
  weights: {"rimworld.*": 1.5, "telegram.group": 0.4}
activate:                          # условия автовыбора (любое из)
  foreground_apps: [rimworldwin64] # имя процесса активного окна, без .exe, нижний регистр
  module_state: {rimworld: {game_running: true}}
priority: 50                       # при нескольких подходящих берётся больший
sticky_s: 120                      # держать профиль столько секунд после пропадания условий
planned: false                     # true: виден на графе, включить нельзя
```

Профили, которые нужно создать (тексты `tone` перенести дословно из `agent/profiles.py`, где они есть):

| id | title | focus_modules | activate | priority | Примечание |
|---|---|---|---|---|---|
| `assistant` | Ассистент | `[desktop, telegram]` | — (по умолчанию) | 0 | тон = `general` из `profiles.py` |
| `work` | Работа и код | `[desktop]` | `foreground_apps` из `profiles.py` (`work`) | 20 | тон `work` |
| `rimworld` | RimWorld | `[rimworld]` | `foreground_apps: [rimworldwin64]`, `module_state: {rimworld: {game_running: true}}` | 50 | тон `game` |
| `editing` | Монтаж | `[montage, desktop]` | `foreground_apps: [davinciresolve, resolve, "premiere pro", vegas]` | 30 | `planned: true` до ТЗ 20; тон написать по образцу (монтаж, не перебивать запись) |
| `minecraft` | Minecraft | `[minecraft]` | — | 50 | `planned: true` |
| `programming` | Программирование | `[code]` | — | 20 | `planned: true` |
| `sleep` | Сон | `[]` | — (особое правило) | — | тон: токенов Claude нет, лёгкий разговор |

Тон профиля `stream` из `profiles.py` не становится профилем: это добавка к тону при включённой настройке `streamer`
(текст перенести в `agent/profiles/_streamer_tone.txt`).

- **CORE-19 (ОБЯЗАТЕЛЬНО).** Выбор активного профиля каждые 2 секунды и при событиях смены активного окна или `state`
  модуля:
  1. `battery.sleeping` → `sleep` (сильнее ручного выбора, как сейчас).
  2. Настройка `profile` ≠ `auto` и такой профиль есть и не `planned` → он.
  3. Иначе все не-`planned` профили, у которых выполнено любое условие `activate` (`foreground_apps` по
     `state.data.foreground_app` модуля `desktop`, а до ТЗ 14 — по `perception.foreground()`; `module_state` — все
     пары ключ-значение совпадают с последним `state.data` модуля), → берётся с наибольшим `priority`; при равенстве —
     текущий, иначе первый по алфавиту.
  4. Если подходящих нет, но текущий профиль перестал подходить меньше `sticky_s` назад — остаётся текущий.
  5. Иначе `assistant`.
- **CORE-20 (ОБЯЗАТЕЛЬНО).** Смена профиля: запись в `events_log` (вид `profile`), тема `profile` всем модулям,
  пересчёт видимости инструментов. О смене профиля она не говорит (как сейчас в `profiles.hint`).
- **CORE-21 (ОБЯЗАТЕЛЬНО).** Подсказка тона в промпт разговора — как сейчас `profiles.hint()`, но из `tone` активного
  профиля (+ добавка стримера). Текст-обёртка `[Внутренняя настройка тона ...]` сохраняется дословно.
- **CORE-22 (ОБЯЗАТЕЛЬНО).** `GET /api/modes` сохраняет формат ответа (`current`, `auto`, `override`, `battery`,
  `nodes`, `edges`, `subgraphs`), чтобы дашборд работал без переделки. `subgraphs[id]` берётся запросом `graph.get` к
  первому модулю из `focus_modules` профиля, у которого `graph: true` (тайм-аут 1,5 с), иначе как сейчас из
  `_DEFAULT_SUBGRAPHS` (перенести в `profile_manager.py`). `POST /api/mode` → настройка `profile`.
- **CORE-23 (ОБЯЗАТЕЛЬНО).** Удалить `modes.py` и `profiles.py`, заменить все импорты. Тесты `test_modes.py` и
  тесты профилей переписать на `profile_manager`.

## 9. Разговор, каналы, речь

### 9.1. Каналы ввода

- **CORE-24 (ОБЯЗАТЕЛЬНО).** Все обращения пользователя к ней становятся одним видом входа
  `UserInput(text, channel, sender, ts)`, где `channel` — `{"kind": "voice"|"dashboard"|"module", "module": str|None,
  "target": str|None}`:
  - чат дашборда (`POST /api/chat`) → `kind=dashboard`;
  - голос (сейчас `MicListener` → `companion.on_voice`) → `kind=voice`;
  - строка быстрого ввода питомца → `kind=dashboard` (как сейчас через чат);
  - события модулей, которые менеджер внимания пометил как обращение владельца к ней (ТЗ 03, правило R1) → `kind=module`
    с `reply_channel` события.
  Обработка — существующий путь компаньона (`on_user_message`/`on_voice`), которому добавляется параметр `channel`.

### 9.2. Каналы ответа

- **CORE-25 (ОБЯЗАТЕЛЬНО).** Ответ на `UserInput` уходит в канал, откуда пришло обращение:
  - `voice` и `dashboard` → как сейчас: журнал, чат дашборда, речь (если не `voice_muted`);
  - `module` → запрос `deliver` модулю `channel.module` с `target`; речь вслух не произносится; если `deliver`
    вернул ошибку или модуль офлайн — ответ пишется в чат дашборда с пометкой в `events_log`.
- **CORE-26 (ОБЯЗАТЕЛЬНО).** Самостоятельные реплики (проактивность, речь модулей, вопросы) идут в речь и чат
  дашборда, как сейчас; в Telegram — только вопросы по правилу ТЗ 01 §7.3.

### 9.3. Вывод речи (`speech_out.py`)

```python
class SpeechOutput(Protocol):
    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None: ...
    async def stop(self, utterance_id: str | None = None) -> None: ...
    def is_speaking(self) -> bool: ...
    def current_segment(self) -> str: ...
```

- **CORE-27 (ОБЯЗАТЕЛЬНО).** `LegacyPetSpeech` — нынешнее поведение `Companion.say` (запись `say.json`, оценка
  `_speak_until` по длине текста). Компаньон говорит только через `SpeechOutput`; весь код, который сейчас пишет
  `say.json` или считает `_speak_until`, переезжает в `LegacyPetSpeech`. Реализация через модуль `voice` — ТЗ 12.
- **CORE-28 (ОБЯЗАТЕЛЬНО).** `utterance_id` = `u-` + 8 hex. Ядро публикует тему `presence` при начале и конце речи
  (`speaking`, `utterance_id`, `segment`, `emotion`), при смене эмоции и при изменении `busy` (идёт обработка
  обращения или выполнение плана).

### 9.4. Заглушка внимания (`attention.py`)

Интерфейс, который ТЗ 03 реализует полностью:

```python
class Attention:
    async def submit(self, module_id: str, event: dict) -> None          # событие модуля
    async def submit_speech(self, module_id: str, say: dict) -> None     # речь модуля (ТЗ 01 §7.2)
    async def submit_question(self, q: "Question") -> Any                 # core.ask и вопросы задач
    async def on_job_update(self, module_id: str, job: dict) -> None
```

- **CORE-29 (ОБЯЗАТЕЛЬНО).** Заглушка этапа 1:
  - `submit`: пишет событие в `events_log` (вид `module_event`); если `addressed_to_me` и `sender.is_owner` и есть
    `reply_channel` — передаёт в разговор как `UserInput(kind=module)`; если событие `memorable` — в очередь
    графа памяти (как `companion._queue_graph`); прочее не озвучивает.
  - `submit_speech`: `mode=verbatim` → `companion.say(text, emotion)`, если сейчас не говорит и пользователь не говорил
    последние 5 с (как сейчас делает агент игры через `speech_status`); `mode=fact` → `companion._speak_about(text)`.
  - `submit_question`: задаёт вопрос через `approvals` (раздел 10.1).
  - `on_job_update`: смена `state` на `done`/`failed`/`waiting_user` → факт для `companion._speak_about`.

## 10. Запросы модулей к ядру

### 10.1. `core.ask`

- **CORE-30 (ОБЯЗАТЕЛЬНО).** `approvals.py` обобщается: `async ask(kind, question, options, timeout_s, source) ->
  (answer, by)`. Существующий `request(text, announce, danger)` остаётся как обёртка над `ask(kind="approval")`.
  Вопросы стоят в очереди: одновременно задаётся один; следующий — после ответа или тайм-аута. Ответ пользователя
  распознаётся как сейчас (`companion._answer_approval`) для `approval`; для `choice` — совпадение с вариантом
  (без регистра, по началу слова; при неоднозначности — переспросить один раз); для `text` — следующая реплика
  пользователя целиком. Кнопки ответа в дашборде (`POST /api/approval` расширить полем `answer`).
- **CORE-31 (ОБЯЗАТЕЛЬНО).** Формулировку вопроса вслух строит модель по факту `question` (через тот же путь, что
  `_speak_about`), а не код.

### 10.2. Прочие запросы

| Запрос | Реализация на этапе 1 |
|---|---|
| `core.tts` | `tts_engine.synth_file(text)` в `asyncio.to_thread` → `{path, duration_ms}` (длительность по WAV-заголовку); `format: ogg` → `opus_encoder`. После ТЗ 12 — перенаправление в `voice` |
| `core.tool.call` | `router.run_tool(tool_id, args)` (проверка параметров и разрешения как сейчас); ошибка → `denied`/`bad_request`/`internal` |
| `core.persona` | `{"name": "ГЛаДОС", "card": ...}`: карточка собирается `prompts.py` из `prompt.yaml` (не из `constitution.yaml`) — новая функция `prompts.persona_card(max_chars=2000)` |
| `core.claude_usage` | `claude_battery.record(tokens, source)` |
| `core.web_search` | `web_search.search(query)` |
| `core.lookup`, `core.memory.*`, `core.experience.add`, `core.kb.ingest` | ответ `unavailable` с сообщением «появится в ТЗ 04» |

- **CORE-32 (ОБЯЗАТЕЛЬНО).** Каждый запрос модуля проверяется по схеме тела (обязательные поля и типы); ошибка →
  `bad_request`.

## 11. Маршрутизатор

- **CORE-33 (ОБЯЗАТЕЛЬНО).** `Router` видит инструменты модулей через реестр окружений (CORE-8, CORE-10) без
  изменений в планировщике, кроме: для инструмента с `job: true` в подсказку добавляется «запускает долгую задачу:
  результат придёт позже»; результат шага такого инструмента — факт «задача <job_id> запущена».
- **CORE-34 (ОБЯЗАТЕЛЬНО).** `STEP_TIMEOUT` для инструмента = `tool.timeout_s` (для встроенных — нынешние 150 с).
- **CORE-35 (ОБЯЗАТЕЛЬНО).** Перед вызовом инструмента с `danger_check` — запрос `tool.check` (тайм-аут 2 с; при
  ошибке или тайм-ауте считать опасным с причиной «модуль не ответил на проверку»).
- **CORE-36 (ЖЕЛАТЕЛЬНО).** Отбор инструментов (`Registry.search`) по эмбеддингам: эмбеддинг описания инструмента
  (EmbeddingGemma через LM Studio, кэш по хешу описания в `memory/tool_embeddings.json`), косинус с запросом, top-k;
  при недоступности эмбеддингов — нынешний отбор по основам слов. Если сделано — тест на заглушке эмбеддингов.

Встроенные инструменты ядра, которые нужно добавить (окружение `core`, `availability: always`):

| id | params | risk | Что |
|---|---|---|---|
| `core.jobs` | — | auto | перечень задач всех модулей (из реестра): модуль, название, состояние, стадия, прогресс; `informative` |
| `core.job_cancel` | `module` (string), `job_id` (string) | ask | отменить задачу (`job.cancel`) |


Существующий инструмент `assistant.set_profile` (в `environments/builtin.py`) не дублировать, а переделать: `enum`
параметра — id профилей из `agent/profiles/` (кроме `planned`) плюс `auto`; пишет настройку `profile`.

## 12. Дашборд

- **CORE-37 (ОБЯЗАТЕЛЬНО).** Новые эндпоинты (только localhost, как остальные):

| Метод и путь | Тело / ответ |
|---|---|
| `GET /api/modules` | `[{id, title, description, version, enabled, connected, configured, status, pid, restarts, last_error, state_summary, tools: [{id, name, description, risk, availability}], capabilities, settings_schema, module_settings}]` |
| `POST /api/modules/{id}/enabled` | `{"enabled": bool}` → запуск или остановка супервизором |
| `POST /api/modules/{id}/restart` | — |
| `POST /api/modules/{id}/settings` | `{"key": str, "value": any}` → проверка по `settings_schema`, сохранение, тема `settings` |
| `GET /api/jobs` | `[{module, job_id, title, state, stage, progress, text, question, artifacts, updated}]` |
| `POST /api/jobs/{module}/{job_id}/cancel` | — |
| `POST /api/jobs/{module}/{job_id}/answer` | `{"question_id": str, "answer": any}` |

- **CORE-38 (ОБЯЗАТЕЛЬНО).** Интерфейс (`static/ui/index.html`): вкладка «Модули» — список модулей (переключатель
  включения, статус, кнопка перезапуска, последняя ошибка, инструменты, форма настроек из `settings_schema`) и список
  задач (состояние, прогресс, вопрос с кнопками ответа, ссылки на артефакты как пути). Стиль — как у существующих
  вкладок (DaisyUI).
- **CORE-39 (ОБЯЗАТЕЛЬНО).** `GET /api/state` дополняется полями `presence` (как тема) и `modules` (краткий список
  `id, connected, enabled`), остальное без изменений — питомец до ТЗ 13 продолжает работать.

## 13. Настройки

- **CORE-40 (ОБЯЗАТЕЛЬНО).** В `settings.DEFAULTS` добавить `"modules": {}` (словарь `id → {"enabled": bool, ...}`).
  `settings.update` для `modules` делает глубокое слияние на один уровень (`modules.<id>` целиком не затирается).
  Значение `profile` теперь — `auto` или id профиля из `agent/profiles/`.
- **CORE-41 (ОБЯЗАТЕЛЬНО).** `core_service._LINKED_SETTINGS` больше не нужен для новых модулей (они получают
  `shared_settings` по манифесту). Для старого агента игры (до ТЗ 11) `/api/speech_status` продолжает отдавать их.

## 14. Совместимость со старым агентом игры (до ТЗ 11)

- **CORE-42 (ОБЯЗАТЕЛЬНО).** Продолжают работать без изменения формата: `POST /api/say`, `GET /api/speech_status`,
  `POST /api/emotion`, `POST /api/event`, `POST /api/tool`, `POST /api/web_search`, `POST /api/claude_usage`,
  `game_bridge.py`, MCP-манифест `environments/manifests/rimworld.json`. Внутри `/api/say` вызывает
  `attention.submit_speech("rimworld", {"text": ..., "mode": "verbatim", "emotion": ...})`. Над каждым таким
  обработчиком комментарий `# устаревший канал агента игры: удалить в ТЗ 11`.

## 15. Тесты

- **CORE-43 (ОБЯЗАТЕЛЬНО).** Новые тесты в `agent/tests/` (без сети, кроме локального aiohttp на свободном порту):
  - шина: рукопожатие с эхо-модулем (из `sdk/examples`), неверный токен → 401, не-петля → 403 (подмена
    `request.remote`), дубликат модуля вытесняет старый, второй `voice.output` отклоняется, неверный манифест →
    `reject`;
  - инструменты: `echo.repeat` виден планировщику в любом профиле (`always`); инструмент с `availability: profile`
    виден только когда модуль в `focus_modules`; вызов через `router.run_tool`; тайм-аут → `ToolError`;
    `danger_check`;
  - задачи: `echo.count` → `job.update` в реестре, вопрос → `approvals.ask` → `job.answer`;
  - профили: каждый шаг алгоритма CORE-19 (сон сильнее ручного; ручной; приоритет; липкость; по умолчанию);
  - супервизор: запуск фиктивного модуля-скрипта, падение → перезапуск с задержкой (время подменяется), 5 падений →
    остановка, выключение → `shutdown` и завершение;
  - каналы: обращение с `kind=module` → `deliver` в модуль; ошибка `deliver` → чат дашборда;
  - `approvals.ask` для `approval`, `choice`, `text`, тайм-аут, очередь из двух вопросов.

## 16. Приёмка

1. `.\check.ps1` зелёный.
2. Ручная проверка, что старое работает: разговор голосом и в чате, питомец показывает и озвучивает, Telegram-канал и
   аккаунт работают, просьба «сделай скриншот игры» выполняется, агент RimWorld (запущенный по-старому из трея)
   говорит, принимает приказы и показывает подграф.
3. Эхо-модуль, включённый в дашборде, запускается супервизором, виден во вкладке «Модули»; просьба в чате «повтори
   через эхо слово кукуруза» выполняется инструментом `echo.repeat`; задача `echo.count` показывает прогресс и вопрос
   во вкладке, ответ кнопкой доходит до модуля.
4. Выключение эхо-модуля в дашборде завершает его процесс за ≤ 10 с.
5. Профиль RimWorld включается при запуске игры, ассистент — после выхода через 120 с.
