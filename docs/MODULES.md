# Модули ГЛаДОС: руководство автора

Модуль это отдельный процесс, который сам подключается к ядру по WebSocket, объявляет свои инструменты и события и
получает вызовы. Внутри модуль устроен как угодно (Python, Node, C#). Здесь короткая версия для Python-модулей.
Полная спецификация протокола, источник правды: [docs/tz/01-module-contract.md](tz/01-module-contract.md). Схема ядра и
словарь: [docs/tz/00-architecture.md](tz/00-architecture.md).

## Как подключиться (5 шагов)

1. **Поставь SDK** в окружение модуля: `pip install -e C:\MyProjects\GitS\sdk` (пакет `gits-sdk`, импорт
   `gits_sdk`, Python 3.12+, зависит только от `aiohttp`).
2. **Опиши манифест**: словарь с `id`, `title`, `description`, `version` и, при необходимости, инструментами, событиями,
   настройками (см. ниже). SDK проверяет его до подключения (`validate_manifest`), ошибки сразу видны в консоли.
3. **Заведи обработчики**: `@client.tool("имя")` на каждый инструмент манифеста (без обработчика `run()` не стартует).
   Отказ с понятной причиной: `raise ToolError("причина", code="unavailable")`.
4. **Запусти** `await client.run()` (или `client.main()` для программы). Адрес ядра `ws://127.0.0.1:8766/module/ws`
   меняется переменной `GITS_CORE_URL`; токен берётся из `GITS_MODULE_TOKEN`, а при ручном запуске из файла
   `%LOCALAPPDATA%\GLaDOS\module_token`, который пишет ядро. Переподключение при обрыве SDK делает сам (1, 2, 4, 8, 16,
   30 секунд).
5. **Шли факты**: `await client.event(kind, text, ...)`, `await client.say(text)`, `await client.state(summary, data)`.
   Дальше решает ядро: важно ли это сейчас и что сказать.

В обычной работе модули запускает супервизор ядра и сам передаёт токен. Руками модуль запускают при разработке (ядро
при этом должно работать) или в тестах против `FakeCore` (ниже).

## Пример манифеста

```python
MANIFEST = {
    "id": "rimworld",                       # латиница, строчные, 2-32 символа
    "title": "RimWorld",
    "description": "Игра-симулятор колонии: колонисты, строительство, исследования, бой.",   # до 300 символов
    "version": "1.0.0",
    "capabilities": ["game"],               # особые роли: voice.output, voice.input, chat.channel, game, ...
    "subscribe": ["presence", "settings", "profile", "module"],     # темы ядра, которые нужны модулю
    "shared_settings": ["streamer", "dnd"],                         # какие общие настройки ядра читает модуль
    "settings_schema": {                    # свои настройки: ядро нарисует форму в дашборде. Секретов тут нет
        "poll_seconds": {"type": "integer", "title": "Опрос игры, секунд", "default": 5, "min": 1, "max": 60},
    },
    "tools": [{
        "name": "order",
        "description": "Передать игроку-агенту приказ по игре своими словами пользователя.",  # для каких просьб
        "params": {"text": {"type": "string", "description": "приказ"}},
        "risk": "auto",                     # auto | ask | public: политика разрешений
        "availability": "profile",          # always: виден в любом профиле; profile: когда модуль в фокусе профиля
        "timeout_s": 15,                    # сколько ядро ждёт ответ (1-120); дольше это задача (job: true)
        "keywords": ["игра", "колония"],
    }],
    "emits": [{"kind": "rimworld.raid", "description": "Начался набег на колонию", "default_urgency": 5}],
    "state": True,                          # модуль шлёт client.state(...)
}
```

Остальные поля инструмента: `informative`, `self_confirming`, `danger_check` (нужен `@client.check`), `job`, `fallback`,
`core_fills` (см. раздел 6.1 спецификации). Опечатки и неверные значения SDK называет по именам полей.

## Пример модуля на 30 строк (эхо)

```python
import asyncio
from gits_sdk import ModuleClient, ToolError

MANIFEST = {
    "id": "echo", "title": "Эхо", "version": "1.0.0",
    "description": "Тестовый модуль: повторяет текст.",
    "subscribe": ["presence"],
    "tools": [{"name": "repeat", "description": "Повторить переданный текст без изменений.",
               "params": {"text": {"type": "string", "description": "что повторить"}},
               "risk": "auto", "availability": "always"}],
    "emits": [{"kind": "echo.started", "description": "Эхо запущено", "default_urgency": 1}],
}
client = ModuleClient(MANIFEST)

@client.tool("repeat")
async def repeat(args):
    if not args["text"].strip():
        raise ToolError("нечего повторять", code="bad_request")
    return args["text"]

@client.on_topic("presence")
async def presence(data):                   # ядро сообщает, говорит ли она сейчас
    print("она говорит:", data.get("speaking"))

@client.on_reconnect
async def reconnected():
    await client.resend_jobs()              # после обрыва повторно отдать незавершённые задачи

async def main():
    task = asyncio.create_task(client.run())
    await client.wait_connected()
    await client.event("echo.started", "Эхо-модуль запущен.")
    raise SystemExit(await task)            # 0: shutdown или вытеснение, 2: ядро отклонило манифест или протокол

asyncio.run(main())
```

Готовый полный пример с задачей (`echo.count`: прогресс, вопрос пользователю на середине, итог) лежит в
[`sdk/examples/echo_module.py`](../sdk/examples/echo_module.py).

## Что умеет клиент

| Вызов | Что делает |
|---|---|
| `await client.event(kind, text, **поля)` | событие для менеджера внимания: `data`, `urgency_hint` 0-5, `addressed_to_me`, `sender`, `reply_channel`, `dedupe_key`, `expires_in_s`, `memorable`, `announce` (объявление о долгом запуске: звучит всегда) |
| `await client.say(text, mode="fact")` | просьба сказать; `mode="verbatim"`, если `text` уже готовая реплика в её характере |
| `await client.emotion(emotion, cause)` | эмоция |
| `await client.state(summary, data)` | сводка для дашборда и выбора профиля (до 300 символов) |
| `await client.ask(question, kind="approval", options=None, timeout_s=300)` | вопрос пользователю; возвращает ответ (`True/False`, строку, текст) |
| `await client.request("core.tts", {...}, timeout=60)` | любой запрос к ядру; `ToolError(code=...)` при ошибке, `asyncio.TimeoutError` без ответа |
| `await client.job_update(job_id, state=..., stage=..., progress=..., text=...)` | ход долгой задачи (`new_job_id()` из `gits_sdk.protocol`); клиент хранит полное состояние и не шлёт прогресс чаще раза в 2 с |
| `client.settings`, `client.module_settings`, `client.enabled`, `client.profile`, `client.battery` | то, что присылает ядро (обновляется само) |
| `core.persona` (запрос) | карточка активного персонажа: `id`, `name`, `aliases`, `gender`, `voice`, `card`, `sections` (разделы `persona.md`); при теме `character` перечитывать (ТЗ 05, CHR-4, CHR-6) |
| `await client.panel.set(blocks)` | пространство модуля в дашборде (ТЗ 15): список блоков, до 64 КБ; не чаще раза в секунду (лишнее сливается), после переподключения шлётся заново |
| `@client.panel_action(id)` | кнопка или строка пространства из `panel.actions` манифеста: `async (args) -> {ok, message?, open_url?}` |

Обработчики: `@client.tool`, `@client.check`, `@client.on_topic`, `@client.on_job_answer`, `@client.on_job_cancel`,
`@client.on_request("speak")` (прочие сообщения ядра), `@client.on_reconnect`, `@client.on_shutdown`.

Выключенный модуль (`client.enabled == False`) остаётся подключённым, но события, речь и эмоции SDK отбрасывает сам.

## Пространство модуля в дашборде (ТЗ 15)

Модуль может показать свою вкладку в «Модули» дашборда. В манифесте объявляется поле `panel` (его ошибки не отклоняют
модуль, а отбрасывают вкладку с предупреждением в журнал ядра):

```json
"panel": {"title": "Монтаж", "icon": "film", "order": 40,
          "links": [{"id": "studio", "label": "Открыть в Remotion Studio", "kind": "tool", "tool": "open_studio",
                     "args_from": "job", "open_result_url": true},
                    {"id": "folder", "label": "Папка роликов", "kind": "reveal_path", "path_from": "output_dir"},
                    {"id": "docs", "label": "Документация", "kind": "url", "url": "https://example.com/docs"}],
          "actions": ["open_studio", "approve"],
          "serve_dirs": ["C:\MyProjects\montage-module\jobs"], "embed_url": "http://127.0.0.1:3000"}
```

- `links`: `url` открывает адрес в новой вкладке; `tool` вызывает инструмент модуля с риском `auto` (аргумент `job_id`, если
  `args_from: "job"`), адрес из результата (`open_result_url`) открывается; `reveal_path` открывает папку проводника, только
  внутри `serve_dirs` (путь - `path` или поле `state.data` с именем `path_from`).
- `serve_dirs`: каталоги, из которых ядро раздаёт артефакты задач (`GET /api/artifact`: видео с перемоткой, `.srt`,
  `.json`, `.md` и т. п.). Без `..` и без симлинков наружу.
- `embed_url`: своя страница модуля на `127.0.0.1` или `localhost`, показывается во вкладке (`iframe`).
- Блоки (`client.panel.set`): `stats`, `table`, `list`, `text`, `progress`, `buttons`, `link`, `image`, `video`, `log`, `tabs`.
  Текст выводится как текст (дашборд экранирует всё, HTML не исполняется). Кнопки и строки таблицы ссылаются на
  `actions` из манифеста, нажатие приходит как `panel.action` (`@client.panel_action`).
- Вкладка видна, пока модуль включён или подключён. Служебная секция «Служебное» (задачи, настройки, инструменты, события)
  есть у каждого модуля.
- Кнопка, которая меняет общую настройку ядра, идёт через `core.tool.call` с инструментом `core.set_setting` (пример: модуль
  `pet`, действия `show` и `hide` меняют `pet_visible`). Модуль может менять только настройки из белого списка
  `MODULE_SETTINGS` (`agent/environments/builtin.py`), значение должно быть `true` или `false`; остальные меняет только человек.
  Белый список сейчас: `telegram`, `telegram_after_run`, `telegram_voice`, `telegram_confirm`, `pet_visible`. Новая настройка в
  список добавляется решением владельца и тестом в `agent/tests/test_telegram_core.py`.

## Политики фокуса (ТЗ 18)

Фокус агента держит один модуль в политике `active`; остальные работают в фоновых политиках. Модуль объявляет свои
политики в манифесте, ядро переводит его между ними (подробно: `docs/FOCUS.md`).

```python
MANIFEST = {
    # ... id, title, description, version, tools, emits, state: True ...
    "policies": [
        {"id": "idle", "title": "Простой", "focus": "background", "transitions": ["working"],
         "interruptible": True, "purpose": "фон, фокус не держит"},
        {"id": "working", "title": "Монтаж", "focus": "active", "transitions": ["idle", "waiting_for_materials"],
         "interruptible": True, "safe_points": ["кадр записан"], "resources": {"cpu": 1}},
        {"id": "waiting_for_materials", "title": "Нет исходников", "focus": "background", "transitions": ["working"]},
    ],
    "default_policy": "idle",
}
```

- `policies` - список; у каждой политики `id` (как у модулей: `^[a-z][a-z0-9_]{1,31}$`), `title`, `focus` (`active` или
  `background`), необязательные `transitions` (пустой - без ограничений), `interruptible` (bool), `safe_points`,
  `resources` (`gpu`, `cpu`, `tokens`, числа от 0), `quiet` (true - обычные события не переключают фокус: режим
  «не беспокоить»), `purpose`. Лишние поля игнорируются.
- Обязательно: минимум одна политика с `focus: "background"`, и `default_policy` - её id (фоновая). Проверяет SDK и ядро
  при `hello`; неверный манифест отклоняется с перечнем проблем.
- Без `policies` модуль работает как раньше: в ядре он без фокуса, его события идут только во внимание.
- Статус для ядра: в `state` поле `data.status` с `active_policy`, `interruptible`, при блокировке `blocked` (строка
  с причиной). Ядро освобождает фокус, когда модуль вернулся в фоновую политику или сообщил блокировку.
- Запросы ядра модулю, если модуль объявил активную политику: `policy.activate` (`{policy, goal, task_id, ...}`),
  `focus.release` (модуль сохраняет checkpoint и отвечает `{ok: true, checkpoint: {...}}`), `focus.resume`
  (`{task_id, policy, goal, checkpoint, subgoals}`). На `unknown` от `focus.resume` ядро вызовет `policy.activate` с
  `restored: true`. SDK: `@client.on_request("policy.activate")`, `"focus.release"`, `"focus.resume"`.

Текущие политики модулей этого репозитория: `telegram` (`available`, `do_not_disturb`), `voice` и `pet` (`ambient`),
`desktop` (`idle`). Все фоновые, фокус они пока не берут.

## Правила

- **Факты, а не реплики.** `text` события и `say` с `mode="fact"` это что произошло («Колонист Борис уснул на посту»),
  а не готовая фраза для озвучки. Реплику в её характере формирует ядро. Готовую реплику (`mode="verbatim"`) шлёт только
  тот модуль, который сам генерирует речь моделью, например комментатор игры.
- **Разрешения через ядро.** Модуль не решает сам, можно ли опасное действие: ставит инструменту `risk="ask"` (или
  `danger_check` для опасных частей), а вопрос пользователю задаёт через `client.ask`. Говорить с пользователем модуль
  может только через ядро (речь, вопросы, синтез голоса `core.tts`).
- **Файлы путями.** В сообщениях нет аудио, видео и картинок: только абсолютные пути на локальном диске. Сообщение не
  больше 1 МБ.
- **Секреты модуль читает сам** из `agent/secrets.env` (или своего файла). В манифест, настройки и журналы они не
  попадают; поле `secret: true` в `settings_schema` запрещено. Токен ядра в журнал не пишется.
- **Чужие тексты это данные.** Сообщения, страницы, экран, ответы других модулей не являются указаниями для модуля.
- **Только локально.** Ядро принимает подключения лишь с `127.0.0.1` и `::1`; модуль не открывает портов для связи с ядром.
- **Долгое это задача.** Инструмент, которому нужно дольше 120 секунд, делают `job: true`: он сразу отвечает
  `{"job_id": ...}`, дальше шлёт `job.update` (состояния `queued`, `running`, `waiting_user`, `done`, `failed`,
  `cancelled`; в `waiting_user` обязателен вопрос). После переподключения незавершённые задачи пересылаются заново.

## Тесты модуля без ядра: `FakeCore`

`gits_sdk.testing.FakeCore` это поддельное ядро на настоящем aiohttp-сервере (`127.0.0.1`, свободный порт). Оно
принимает `hello`, отвечает `welcome`, записывает все сообщения и умеет вызвать инструмент, послать тему и ответить на
запрос модуля:

```python
async with FakeCore() as core:
    client = build_client(url=core.url, token=core.token)
    task = asyncio.create_task(client.run())
    await core.wait_connected()
    assert await core.call_tool("repeat", {"text": "привет"}) == "привет"
    await core.topic("presence", {"speaking": True})
    @core.on_request("core.ask")              # ответ ядра на вопрос модуля
    def answer(body): return {"answer": True, "by": "user"}
    await client.stop()
```

Полезное: `core.events`, `core.messages_of("say")`, `await core.wait_message("job.update", предикат)`,
`core.job_answer(...)`, `core.drop()` (оборвать соединение), `core.kick()` (закрыть кодом 4000), `core.set_enabled(False)`.
Примеры использования: `sdk/tests/`.

## Как ядро запускает модуль и что с ним делает

- **Запуск.** Модули из `agent/modules.yaml` ядро запускает само (супервизор): `cwd`, `command`, `args`, `autostart`, `restart`
  (`always`, `on-failure`, `never`), `enabled` (включён ли, пока пользователь не выбрал сам). В путях и аргументах подставляются
  `{GITS}`, `{PYTHON}` и переменные окружения ядра в фигурных скобках; нет переменной - модуль помечен «не настроен».
  Процессу передаются `GITS_MODULE_TOKEN`, `GITS_CORE_URL`, `GITS_MODULE_ID`; до установки пакета `gits-sdk` в
  `PYTHONPATH` добавляется `GitS/sdk`. Вывод процесса лежит в `agent/logs/modules/<id>.log`.
- **Сервисы.** Запись `kind: service` с `health_url` (например классификатор `decider`) это процесс без WebSocket: ядро считает его
  работающим, пока `GET health_url` отвечает 200 (опрос раз в 10 секунд), перезапускает по `restart` и не запускает второй
  экземпляр, если порт уже занят работающим сервисом. Токен ядра сервису не передаётся, вкладка «Модули» помечает его «сервис».
- **Внимание.** Событие модуля оценивается менеджером внимания (ТЗ 03): `urgency_hint` и `addressed_to_me` ускоряют оценку (при
  обоих и без `sender` классификатор не вызывается), а `kind` с `default_urgency: 5` в `emits` и `urgency_hint: 5` считается
  критичным без классификатора. Речь модуля (`say`) проходит ту же оценку; готовая реплика (`verbatim`) живёт 20 секунд.
- **Падения.** Перезапуск через 2, 5, 15, 60 с; пять падений за десять минут - модуль помечается «остановлен после сбоев»
  (последние строки вывода видны в дашборде), кнопка «перезапустить» сбрасывает счётчик.
- **Включение.** Вкладка «Модули» дашборда: переключатель шлёт модулю тему `module` (`client.enabled`), а для запущенного
  ядром процесса ещё и `shutdown` (на выход 10 с, потом процесс убивают вместе с потомками). Вручную запущенный модуль
  принимается как обычный, второй экземпляр ядро не запускает.
- **Инструменты и профиль.** Инструмент с `availability: always` виден планировщику всегда, с `profile` - когда модуль входит в
  `focus_modules` активного профиля (`agent/profiles/*.yaml`). Фоновые задачи модуля идут независимо от профиля.
- **Настройки.** `settings_schema` рисуется формой во вкладке «Модули», значения хранятся в `settings.json` под
  `modules.<id>.<имя>` и приходят в `client.module_settings` (тема `settings`). Имя `enabled` занято ядром, id `core` тоже.
- **Задачи и вопросы.** `job.update` видны во вкладке «Модули»; вопрос в состоянии `waiting_user` ядро задаёт один раз на
  `question_id` и вслух, и кнопками, ответ приходит `job.answer`. Тот же вопрос из `client.ask` попадает в общую очередь:
  одновременно пользователю задаётся один вопрос.

## Модули в монорепо

Модули GitS лежат в `modules/<id>/` (образец: `modules/telegram/`): `main.py`, `manifest.py`, свои файлы, `tests/`. Супервизор запускает их с `cwd` модуля и передаёт `MEMORY_DIR` (абсолютный путь папки памяти ядра); пакет `gits-sdk` берётся из `PYTHONPATH` (`GitS/sdk`) или из окружения, если он установлен. В `command` и `args` кроме `{PYTHON}` доступен `{PYTHONW}` (pythonw.exe рядом с интерпретатором ядра, для модулей с окном).

- **`voice`** (ТЗ 12): capabilities `voice.output` и `voice.input`; принимает от ядра `speak` (`utterance_id`, `text`, `emotion`, `replace`) и `speak.stop` (`utterance_id` или `null`, `at: now | segment_end`), отвечает на `tts.synthesize` (`text`, `emotion`, `format: wav | ogg` → `path`, `duration_ms`; файлы в `%TEMP%\glados_voice\`), шлёт события `voice.speech` (`phase: started | segment | ended`, `utterance_id`, `seq`, `segment`, `completed`, `spoken_upto`, `reason`), `voice.utterance`, `voice.user_speaking`, `voice.heard`, `voice.named`. Настройки: `barge_in`, `echo_tail_ms`. `GITS_VOICE_FAKE=1` — без звука.
  Распознавание речи (`stt_client.py`, `local_stt.py`): основной путь — WhisperX (`WHISPERX_URL`, с диаризацией и голосовыми
  отпечатками). При старте и раз в 60 с проверяется `/health`; если сервер не отвечает (в том числе при ошибке соединения во время
  фразы), распознаём локально faster-whisper на процессоре (модель `GITS_STT_MODEL`, по умолчанию small, int8, потоков не больше
  `GITS_STT_THREADS`, по умолчанию 2). Модель одна на процесс: её делят микрофон и слух (`hearing.py`). Веса только из кэша
  (`local_files_only`): если их нет, модуль этого честно пишет в состояние и журнал и НЕ скачивает их сам. Локальный режим без
  диаризации: голосов не узнаёт, перебивание (`barge_in`) и образец её голоса отключены, эхо режется только закрытием «ворот»
  микрофона. Состояние и панель «Голос» показывают режим («WhisperX» или «локально (CPU)»); переход логируется один раз.
- **`pet`** (ТЗ 13): capabilities `presence.view`, подписка на `presence`, `settings`, `module`; инструментов и событий нет. Окно в главном потоке, клиент в отдельном (`queue.Queue`); свои изменения настроек (глушилка, позиция, видимость, закрепление) отправляет через `POST /api/settings` дашборда. `GITS_PET_HEADLESS=1` — без окна.

## Модули в отдельных репозиториях

Тяжёлые модули живут в своих репозиториях со своими зависимостями: первый, `rimworld-module` (игра RimWorld, ТЗ 11). Запись в
`agent/modules.yaml` ссылается на его папку переменной (`{RIMWORLD_MODULE_DIR}`, ядро находит её само: `agent/module_dirs.py`),
интерпретатор общий (`{PYTHON}`), если зависимостей модуля хватает в нём. Такой модуль хранит данные в своей папке, а не в
`MEMORY_DIR` ядра (супервизор передаёт `MEMORY_DIR` всем, модуль RimWorld заменяет его своим `agent/memory`). Проверка у него своя:
`.\check.ps1` в его репозитории, а тесты против `FakeCore` из этого SDK. Пример манифеста с `capabilities: ["game"]`, инструментом `order`
(приказ игроку-агенту: ядро зовёт его у модулей фокуса с этой возможностью), `graph: true` (подграф профиля) и событиями: выше и в
`agent/tests/test_rimworld_core.py`.

## Проверка

Из корня репозитория `.\check.ps1` запускает ruff и тесты ядра, SDK и модулей. Только SDK: `python -m pytest -q sdk/tests`; модуль Telegram: `python -m pytest -q modules/telegram/tests`; голос: `python -m pytest -q modules/voice/tests`; питомец: `python -m pytest -q modules/pet/tests` (тесты каждого модуля идут отдельным запуском).
