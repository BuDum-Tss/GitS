# Окружения и сервисы: руководство автора

Участник шины это отдельный процесс, который сам подключается к ядру по WebSocket, объявляет свои инструменты и события и
получает вызовы. Внутри участник шины устроен как угодно (Python, Node, C#). Здесь короткая версия для Python-участников шины.
Полная спецификация протокола, источник правды: [docs/MODULE_CONTRACT.md](MODULE_CONTRACT.md). Схема ядра и
схема ядра: [docs/architecture-clean.html](architecture-clean.html).
**Все запросы, сообщения, темы, переменные окружения и ресурсы со всеми полями и кодами ошибок:
[docs/MODULE_CONTRACT.md](MODULE_CONTRACT.md)** (создаётся из кода скриптом `python agent/interfaces/workspace/tools/gen_contract_doc.py`, схемы в
`docs/contract/*.json`). Участник шины зависит только от SDK: журнал `client.log(...)`, локальная модель `gits_sdk.host.llm_ask` /
`llm_json`, типизированные запросы `client.tts`, `persona`, `call_tool`, `claude_usage` и др.; внутренности `agent/` не
импортируются (тест `test/test_architecture.py`).

## Словарь: workspace и service (контракт 2.0)

Участники шины делятся на два рода (слова «участник шины» в контракте больше нет):

| Род | Что это | Примеры | Клиент SDK |
|---|---|---|---|
| **workspace** (окружение) | инструменты, события и рабочее место агента (`workspace`, `nodes` в манифесте) | `montage`, `rimworld`, `minecraft`, `desktop`, `telegram`, `streamer` (Twitch и OBS одним окружением) | `WorkspaceClient` |
| **service** | подключается к ядру по WebSocket; окружения и аренды нет, инструменты допустимы | `pet`, `voice` | `ServiceClient` |

Внешние сервисы без WebSocket (decider, espeech и т. п.) тоже сервисы, но к SDK не относятся. Общая база клиентов: `PeerClient`.

Поле манифеста **`role`** обязательно: `"workspace"` или `"service"`; без него манифест отвергается. Поле **`ownership`**
обязательно у workspace: `"shared"` (общий, эксклюзивный, берётся в аренду: montage, rimworld, minecraft, desktop) или `"owned"`
(живёт внутри ядра персонажа: telegram, streamer). Service с `ownership` или с описанием `workspace` отвергается проверкой манифеста.
`WorkspaceClient` принимает только `role: workspace`, `ServiceClient` только `role: service`.

```python
from gits_sdk import ServiceClient, WorkspaceClient
client = WorkspaceClient({..., "role": "workspace", "ownership": "shared"})     # окружение в аренду
pet = ServiceClient({"id": "pet", ..., "role": "service"})                      # сервис
```

Контракт 2.0 (`PROTOCOL` 2) убрал алиасы фаз 1-3: прежний универсальный клиент, прежнее поле `hello` рядом с `id`, прежний адрес входа,
прежние переменные окружения, прежний раздел `peers.yaml` и общий адрес списка дашборда. Остались `WorkspaceClient`, `ServiceClient`,
`hello` с `id` и `role`, адрес `/ws`, переменные `GITS_ID`, `GITS_TOKEN`, `GITS_CORE_URL`, `GITS_CONTROL_TOKEN`, `GITS_PEERS_FILE`, разделы
`workspaces:`, `services:`, `planned:` и адреса `/api/workspaces`, `/api/services`, `/api/peers/<id>/...`. Список удалённого поимённо: история
контракта в [docs/MODULE_CONTRACT.md](MODULE_CONTRACT.md).

### Аренда общего workspace (`ownership: shared`)

Общее окружение (пространство: целый контейнер) занимает один хозяин (ядро персонажа) за раз, как семафор. Хозяин берёт его в
аренду по HTTP-интерфейсу управления, workspace подключается к ядру хозяина по WebSocket. У аренды нет срока и нет продления:
она держится, пока хозяин не вызовет `release` (выход агента из окружения) или пока не оборвётся WebSocket участника (ядро упало
или отключило его): тогда workspace сам отпускает аренду. Сервер и клиент живут в `gits_sdk.control`:

```python
from gits_sdk.control import ControlServer, LeaseClient

# сторона workspace: токен управления из GITS_CONTROL_TOKEN (без него сервер не создаётся)
server = ControlServer(id="montage", manifest=MANIFEST, on_acquire=connect_to_owner, on_release=disconnect)   # lease.core_url, lease.token
await server.start("0.0.0.0", 8780)
# клиент, созданный в on_acquire: client.on_disconnect(lambda: server.connection_lost(lease.lease_id))

# сторона ядра
leases = LeaseClient("http://montage:8780", control_token)
about = await leases.info()          # для чего пространство и когда в него переходить: до аренды и без подключения к ядру
lease = await leases.acquire(character="glados", core_url=core_url, token=core_token, purpose="montage")
await leases.release(lease["lease_id"])
```

- `GET /status`: `{id, role, ownership, state: offline|free|busy, holder, active}`; `POST /acquire`, `/release`. Один хозяин за раз
  (другому 409 с описанием хозяина), повторный `acquire` того же `character` и `purpose` идемпотентен.
- Выход с работой: если при `release` у workspace есть незаконченные задачи, `on_release(lease)` возвращает `False` (`lease.why` даёт причину:
  `release`, `finished`, `disconnect`, `stop`). Тогда `POST /release` отвечает `{"ok": true, "released": false, "pending": "jobs"}`, аренда
  остаётся (`holder.releasing: true`, `holder.jobs`), связь с ядром не рвётся, другой персонаж получает 409, тот же `acquire` возвращает аренду
  (тот же `lease_id`). Когда задачи кончились, workspace зовёт `server.finish_release(lease_id)`. Обрыв WebSocket закрывает аренду сразу.
- `GET /info`: описание пространства из манифеста (`title`, `purpose`, `when_to_use`, `keywords`, `entry`, `interruptible`,
  `safe_points`, `done_when`, `tools`) и состояние аренды. Состояние аренды хранит сам workspace, ядро его не дублирует.
- Участники без аренды (`ownership: owned` и services) отвечают на `GET /status` и `GET /info`, остальное 404.
- Поля, коды ответов и примеры: раздел «Интерфейс управления» в [docs/MODULE_CONTRACT.md](MODULE_CONTRACT.md).

## Как подключиться (5 шагов)

1. **Поставь SDK** в окружение участника шины: `pip install -e C:\MyProjects\GitS\sdk` (пакет `gits-sdk`, импорт
   `gits_sdk`, Python 3.12+, зависит только от `aiohttp`).
2. **Опиши манифест**: словарь с `id`, `title`, `description`, `version` и, при необходимости, инструментами, событиями,
   настройками (см. ниже). SDK проверяет его до подключения (`validate_manifest`), ошибки сразу видны в консоли.
3. **Заведи обработчики**: `@client.tool("имя")` на каждый инструмент манифеста (без обработчика `run()` не стартует).
   Отказ с понятной причиной: `raise ToolError("причина", code="unavailable")`.
4. **Запусти** `await client.run()` (или `client.main()` для программы). Адрес ядра `ws://127.0.0.1:8766/ws`
   меняется переменной `GITS_CORE_URL`; токен берётся из `GITS_TOKEN`, а при ручном запуске из файла
   `%LOCALAPPDATA%\GLaDOS\module_token`, который пишет ядро. Переподключение при обрыве SDK делает сам (1, 2, 4, 8, 16,
   30 секунд).
5. **Шли факты**: `await client.event(kind, text, ...)`, `await client.say(text)`, `await client.state(summary, data)`.
   Дальше решает ядро: важно ли это сейчас и что сказать.

В обычной работе участники шины запускает супервизор ядра и сам передаёт токен. Руками участник шины запускают при разработке (ядро
при этом должно работать) или в тестах против `FakeCore` (ниже).

## Пример манифеста

```python
MANIFEST = {
    "id": "rimworld",                       # латиница, строчные, 2-32 символа
    "role": "workspace",                    # обязательно: workspace | service
    "ownership": "shared",                  # обязательно у workspace: shared | owned
    "title": "RimWorld",
    "description": "Игра-симулятор колонии: колонисты, строительство, исследования, бой.",   # до 300 символов
    "version": "1.0.0",
    "capabilities": ["game"],               # особые роли: voice.output, voice.input, chat.channel, game, ...
    "subscribe": ["presence", "settings", "profile", "module"],     # темы ядра, которые нужны участнику шины
    "shared_settings": ["streamer", "dnd"],                         # какие общие настройки ядра читает участник шины
    "settings_schema": {                    # свои настройки: ядро нарисует форму в дашборде. Секретов тут нет
        "poll_seconds": {"type": "integer", "title": "Опрос игры, секунд", "default": 5, "min": 1, "max": 60},
    },
    "tools": [{
        "name": "order",
        "description": "Передать игроку-агенту приказ по игре своими словами пользователя.",  # для каких просьб
        "params": {"text": {"type": "string", "description": "приказ"}},
        "risk": "auto",                     # auto | ask | public: политика разрешений
        "availability": "profile",          # always: виден в любом профиле; profile: когда участник шины в фокусе профиля
        "timeout_s": 15,                    # сколько ядро ждёт ответ (1-120); дольше это задача (job: true)
        "keywords": ["игра", "колония"],
    }],
    "emits": [{"kind": "rimworld.raid", "description": "Начался набег на колонию", "default_urgency": 5}],
    "state": True,                          # участник шины шлёт client.state(...)
}
```

Остальные поля инструмента: `informative`, `self_confirming`, `danger_check` (нужен `@client.check`), `job`, `fallback`,
`core_fills` (см. раздел 6.1 спецификации). Опечатки и неверные значения SDK называет по именам полей.

## Пример окружения на 30 строк (эхо)

```python
import asyncio
from gits_sdk import ToolError, WorkspaceClient

MANIFEST = {
    "id": "echo", "role": "workspace", "ownership": "owned", "title": "Эхо", "version": "1.0.0",
    "description": "Тестовое окружение: повторяет текст.",
    "subscribe": ["presence"],
    "tools": [{"name": "repeat", "description": "Повторить переданный текст без изменений.",
               "params": {"text": {"type": "string", "description": "что повторить"}},
               "risk": "auto", "availability": "always"}],
    "emits": [{"kind": "echo.started", "description": "Эхо запущено", "default_urgency": 1}],
}
client = WorkspaceClient(MANIFEST)

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
    await client.event("echo.started", "Эхо-участник шины запущен.")
    raise SystemExit(await task)            # 0: shutdown или вытеснение, 2: ядро отклонило манифест или протокол

asyncio.run(main())
```

Готовый полный пример с задачей (`echo.count`: прогресс, вопрос пользователю на середине, итог) лежит в
[`agent/interfaces/workspace/examples/echo_module.py`](../sdk/examples/echo_module.py). Заготовку нового участника шины с тестами на `FakeCore` берите из
[`agent/interfaces/workspace/template/`](../sdk/template/README.md).

## Что умеет клиент

| Вызов | Что делает |
|---|---|
| `await client.event(kind, text, **поля)` | событие для менеджера внимания: `data`, `urgency_hint` 0-5, `addressed_to_me`, `sender`, `reply_channel`, `dedupe_key`, `expires_in_s`, `memorable`, `announce` (объявление о долгом запуске: звучит всегда) |
| `await client.say(text, mode="fact")` | просьба сказать; `mode="verbatim"`, если `text` уже готовая реплика в её характере |
| `await client.emotion(emotion, cause)` | устарело (контракт 1.7): ядро игнорирует; стикер выбирает тег в тексте ответа |
| `await client.state(summary, data)` | сводка для дашборда и выбора профиля (до 300 символов) |
| `await client.ask(question, kind="approval", options=None, timeout_s=300)` | вопрос пользователю; возвращает ответ (`True/False`, строку, текст) |
| `await client.request("core.tts", {...}, timeout=60)` | любой запрос к ядру; `ToolError(code=...)` при ошибке, `asyncio.TimeoutError` без ответа |
| `await client.job_update(job_id, state=..., stage=..., progress=..., text=...)` | ход долгой задачи (`new_job_id()` из `gits_sdk.protocol`); клиент хранит полное состояние и не шлёт прогресс чаще раза в 2 с |
| `client.settings`, `client.module_settings`, `client.enabled`, `client.profile`, `client.battery` | то, что присылает ядро (обновляется само) |
| `core.persona` (запрос) | карточка активного персонажа: `id`, `name`, `aliases`, `gender`, `voice`, `card`, `sections` (разделы `persona.md`); при теме `character` перечитывать (ТЗ 05, CHR-4, CHR-6) |
| `await client.panel.set(blocks)` | пространство участника шины в дашборде (ТЗ 15): список блоков, до 64 КБ; не чаще раза в секунду (лишнее сливается), после переподключения шлётся заново |
| `@client.panel_action(id)` | кнопка или строка пространства из `panel.actions` манифеста: `async (args) -> {ok, message?, open_url?}` |

Обработчики: `@client.tool`, `@client.check`, `@client.on_topic`, `@client.on_job_answer`, `@client.on_job_cancel`,
`@client.on_request("speak")` (прочие сообщения ядра), `@client.on_reconnect`, `@client.on_shutdown`.

Выключенный участник шины (`client.enabled == False`) остаётся подключённым, но события, речь SDK отбрасывает сам.

## Пространство участника в дашборде (ТЗ 15)

Окружение может показать свою вкладку в «Окружения» или «Сервисы» дашборда. В манифесте объявляется поле `panel` (его ошибки не отклоняют
окружение, а отбрасывают вкладку с предупреждением в журнал ядра):

```json
"panel": {"title": "Монтаж", "icon": "film", "order": 40,
          "links": [{"id": "studio", "label": "Открыть в Remotion Studio", "kind": "tool", "tool": "open_studio",
                     "args_from": "job", "open_result_url": true},
                    {"id": "folder", "label": "Папка роликов", "kind": "reveal_path", "path_from": "output_dir"},
                    {"id": "docs", "label": "Документация", "kind": "url", "url": "https://example.com/docs"}],
          "actions": ["open_studio", "approve"],
          "serve_dirs": ["C:\MyProjects\montage-module\jobs"], "embed_url": "http://127.0.0.1:3000"}
```

- `links`: `url` открывает адрес в новой вкладке; `tool` вызывает инструмент окружения с риском `auto` (аргумент `job_id`, если
  `args_from: "job"`), адрес из результата (`open_result_url`) открывается; `reveal_path` открывает папку проводника, только
  внутри `serve_dirs` (путь - `path` или поле `state.data` с именем `path_from`).
- `serve_dirs`: каталоги, из которых ядро раздаёт артефакты задач (`GET /api/artifact`: видео с перемоткой, `.srt`,
  `.json`, `.md` и т. п.). Без `..` и без симлинков наружу.
- `embed_url`: своя страница окружения на `127.0.0.1` или `localhost`, показывается во вкладке (`iframe`).
- Блоки (`client.panel.set`): `stats`, `table`, `list`, `text`, `progress`, `buttons`, `link`, `image`, `video`, `log`, `tabs`.
  Текст выводится как текст (дашборд экранирует всё, HTML не исполняется). Кнопки и строки таблицы ссылаются на
  `actions` из манифеста, нажатие приходит как `panel.action` (`@client.panel_action`).
- Вкладка видна, пока окружение включён или подключён. Служебная секция «Служебное» (задачи, настройки, инструменты, события)
  есть у каждого окружения.
- Кнопка, которая меняет общую настройку ядра, идёт через `core.tool.call` с инструментом `core.set_setting`. Участник может менять
  только настройки из белого списка `MODULE_SETTINGS` (`agent/environments/builtin.py`), значение должно быть `true` или `false`;
  остальные меняет только человек. Белый список сейчас: `autoplay`. Новая настройка в список добавляется решением владельца и тестом в `test/test_telegram_core.py`.
- Собственные настройки участника (окно, позиция, клавиши) лежат в его `settings_schema` и хранятся в `peers.<id>` (ядро их не
  знает). Меняются через дашборд (`POST /api/peers/<id>/settings`): так делают кнопки «Показать» и «Скрыть» сервиса `pet`, запись
  позиции его окна и инструменты `telegram.set_periodic` / `telegram.set_after_run` (настройки Telegram `publish`, `after_run`, `voice`,
  `confirm` хранятся в `peers.telegram`; старые общие ключи `telegram*` при загрузке `settings.json` переносятся туда).

### Среда агента (ТЗ 32)

Окружение, в который агент может «зайти» (игра, монтаж), объявляет среду полем `workspace`:

```python
"workspace": {"id": "montage", "title": "Монтаж", "purpose": "монтаж роликов",
              "entry": ["user_message", "self_impulse", "critical"], "interruptible": True,
              "safe_points": ["между стадиями задачи"], "signals_in": ["telegram.*"], "done_when": "задачи готовы",
              "when_to_use": "когда просят смонтировать ролик из файлов", "keywords": ["монтаж", "ролик"]}
```

- `id` равен id окружения, `title` обязателен. `entry` - как можно войти: `user_message`, `self_impulse`, `schedule`, `critical`,
  `fallback`; по умолчанию первые два и `critical`. Запуск процесса, окно и состояние окружения среду не включают.
- `when_to_use` (строка) и `keywords` (список) необязательны: когда в пространство переходить; их отдаёт `GET /info` до аренды.
- `signals_in` - какие чужие события (шаблоны видов) среда принимает уведомлениями; остальные копятся в сводке. Критическое
  событие (срочность 5) переключает среду по правилам без LLM, если среда его окружения разрешает `critical`; текущая среда вправе
  отказать в небезопасной точке (`status.interruptible: false`).
- Вход в среду не требует отдельного состояния окружения: достаточно подключённого окружения. Запросы ядра: `workspace.enter`
  (`{workspace, goal, task_id, reason}`; окружение может отказать ответом `{ok: false, error}`), `workspace.exit` (окружение
  сохраняет checkpoint и отвечает `{ok: true, checkpoint: {...}}`), `workspace.resume` (`{task_id, goal, checkpoint, subgoals}`).
  SDK: `@client.on_request("workspace.enter")` и так далее; в тестах `FakeCore.workspace("enter" | "exit" | "resume")`.
- Статус для ядра: в `state` поле `data.status` с `interruptible` и флагами окончания: `done: true` (задача кончилась) и
  `blocked` (строка с причиной). Среда кончается, когда окружение сообщил `done` или `blocked` либо отключился: агент
  возвращается в `assistant`.
- Устаревшие поля манифеста о политиках игнорируются как неизвестное (MOD-10), регистрацию не ломает.

Среды объявляют `rimworld` и `montage`; `telegram`, `voice`, `pet` и `desktop` среды не объявляют.

## Рабочее место: контентные окружения (ТЗ 33)

Окружение, в котором агент «живёт» (игра, мессенджер, мир), строится как рабочее место: инструменты образуют дерево узлов, повторяющее
интерфейс предметной области. **Нормативный документ с правилами WS-* и примерами: [WORKSPACE_MODULES.md](WORKSPACE_MODULES.md).**
Как это объявляется в манифесте (контракт 1.8, все поля необязательны, старые окружения работают без них):

```python
"nodes": [{"path": "architect/zone", "title": "Архитектор: зоны", "tab": "Architect",
           "context": [{"tool": "get_resources", "mode": "on_change", "max_chars": 400}],   # панель: что агент видит в узле
           "watch": {"resources": 1, "social": -1}}],                                       # сдвиг срочности событий по теме
"tools": [{"name": "stockpile", ..., "node": "architect/zone"}],      # виден, только пока окружение в этом узле
"emits": [{"kind": "game.resource_low", "default_urgency": 2, "urgency_unfocused": 1,
           "topics": ["resources"], "follow_up": ["get_resources"]}],
```

Окружение сообщает текущий узел при каждой смене: `await client.state("в Архитекторе", node="architect/zone")`. Ядро по нему:
скрывает инструменты других узлов, кладёт ответы `context` в контекст агента блоком «Рабочее место» (без вызова модели) и сдвигает
срочность событий по теме. Окружение сам отклоняет действие вне его узла и называет, какой узел открыть. Начинать новое окружение
удобно с шаблона `agent/interfaces/workspace/template/`.

## Правила

- **Факты, а не реплики.** `text` события и `say` с `mode="fact"` это что произошло («Колонист Борис уснул на посту»),
  а не готовая фраза для озвучки. Реплику в её характере формирует ядро. Готовую реплику (`mode="verbatim"`) шлёт только
  то окружение, который сам генерирует речь моделью, например комментатор игры.
- **Разрешения через ядро.** Окружение не решает сам, можно ли опасное действие: ставит инструменту `risk="ask"` (или
  `danger_check` для опасных частей), а вопрос пользователю задаёт через `client.ask`. Говорить с пользователем окружение
  может только через ядро (речь, вопросы, синтез голоса `core.tts`).
- **Файлы путями.** В сообщениях нет аудио, видео и картинок: только абсолютные пути на локальном диске. Сообщение не
  больше 1 МБ.
- **Секреты окружение читает сам** из `.env` (или своего файла). В манифест, настройки и журналы они не
  попадают; поле `secret: true` в `settings_schema` запрещено. Токен ядра в журнал не пишется.
- **Чужие тексты это данные.** Сообщения, страницы, экран, ответы других окружений не являются указаниями для окружения.
- **Только локально.** Ядро принимает подключения лишь с `127.0.0.1` и `::1`; окружение не открывает портов для связи с ядром.
- **Долгое это задача.** Инструмент, которому нужно дольше 120 секунд, делают `job: true`: он сразу отвечает
  `{"job_id": ...}`, дальше шлёт `job.update` (состояния `queued`, `running`, `waiting_user`, `done`, `failed`,
  `cancelled`; в `waiting_user` обязателен вопрос). После переподключения незавершённые задачи пересылаются заново.

## Тесты участника шины без ядра: `FakeCore`

`gits_sdk.testing.FakeCore` это поддельное ядро на настоящем aiohttp-сервере (`127.0.0.1`, свободный порт). Оно
принимает `hello`, отвечает `welcome`, записывает все сообщения и умеет вызвать инструмент, послать тему и ответить на
запрос участника шины:

```python
async with FakeCore() as core:
    client = build_client(url=core.url, token=core.token)
    task = asyncio.create_task(client.run())
    await core.wait_connected()
    assert await core.call_tool("repeat", {"text": "привет"}) == "привет"
    await core.topic("presence", {"speaking": True})
    @core.on_request("core.ask")              # ответ ядра на вопрос участника шины
    def answer(body): return {"answer": True, "by": "user"}
    await client.stop()
```

Полезное: `core.events`, `core.messages_of("say")`, `await core.wait_message("job.update", предикат)`,
`core.job_answer(...)`, `core.drop()` (оборвать соединение), `core.kick()` (закрыть кодом 4000), `core.set_enabled(False)`.
Примеры использования: `agent/interfaces/workspace/tests/`.

## Как ядро запускает участника шины и что с ним делает

- **Запуск.** Участников из `agent/peers.yaml` (разделы `workspaces:` и `services:`) ядро запускает само (супервизор): `cwd`, `command`, `args`, `autostart`, `restart`
  (`always`, `on-failure`, `never`), `enabled` (включён ли, пока пользователь не выбрал сам). В путях и аргументах подставляются
  `{GITS}`, `{PYTHON}` и переменные окружения ядра в фигурных скобках; нет переменной - участник шины помечен «не настроен».
  Процессу передаются `GITS_TOKEN`, `GITS_CORE_URL`, `GITS_ID`; до установки пакета `gits-sdk` в
  `PYTHONPATH` добавляется `GitS/sdk`. Вывод процесса лежит в `logs/modules/<id>.log`.
- **Сервисы.** Запись `kind: service` с `health_url` (например классификатор `decider`) это процесс без WebSocket: ядро считает его
  работающим, пока `GET health_url` отвечает 200 (опрос раз в 10 секунд), перезапускает по `restart` и не запускает второй
  экземпляр, если порт уже занят работающим сервисом. Токен ядра сервису не передаётся, вкладка «Участники шины» помечает его «сервис».
- **Внимание.** Событие участника шины оценивается менеджером внимания (ТЗ 03): `urgency_hint` и `addressed_to_me` ускоряют оценку (при
  обоих и без `sender` классификатор не вызывается), а `kind` с `default_urgency: 5` в `emits` и `urgency_hint: 5` считается
  критичным без классификатора. Речь участника шины (`say`) проходит ту же оценку; готовая реплика (`verbatim`) живёт 20 секунд.
- **Падения.** Перезапуск через 2, 5, 15, 60 с; пять падений за десять минут - участник шины помечается «остановлен после сбоев»
  (последние строки вывода видны в дашборде), кнопка «перезапустить» сбрасывает счётчик.
- **Включение.** Вкладки «Окружения» и «Сервисы» дашборда: переключатель шлёт участнику шины тему `module` (`client.enabled`), а для запущенного
  ядром процесса ещё и `shutdown` (на выход 10 с, потом процесс убивают вместе с потомками). Вручную запущенный участник шины
  принимается как обычный, второй экземпляр ядро не запускает.
- **Инструменты и профиль.** Инструмент с `availability: always` виден планировщику всегда, с `profile` - когда участник шины входит в
  `focus` текущей среды (`agent/services/spaces/space_config.yaml`). Фоновые задачи участника шины идут независимо от профиля.
- **Настройки.** `settings_schema` рисуется формой во вкладках «Окружения» и «Сервисы», значения хранятся в `settings.json` под
  `peers.<id>.<имя>` и приходят в `client.module_settings` (тема `settings`). Имя `enabled` занято ядром, id `core` тоже.
- **Задачи и вопросы.** `job.update` видны во вкладках «Окружения» и «Сервисы»; вопрос в состоянии `waiting_user` ядро задаёт один раз на
  `question_id` и вслух, и кнопками, ответ приходит `job.answer`. Тот же вопрос из `client.ask` попадает в общую очередь:
  одновременно пользователю задаётся один вопрос.

## Участники в монорепо

Участники GitS лежат в `workspaces/<id>/` (окружения) и `services/<id>/` (сервисы; образец: `workspaces/telegram/`): `main.py`, `manifest.py`, свои файлы, `tests/`. Супервизор запускает их с `cwd` участника шины и передаёт `MEMORY_DIR` (абсолютный путь папки памяти ядра); пакет `gits-sdk` берётся из `PYTHONPATH` (`GitS/sdk`) или из окружения, если он установлен. В `command` и `args` кроме `{PYTHON}` доступен `{PYTHONW}` (pythonw.exe рядом с интерпретатором ядра, для участников шины с окном).

- **`voice`** (ТЗ 12): capabilities `voice.output` и `voice.input`; принимает от ядра `speak` (`utterance_id`, `text`, `replace`) и `speak.stop` (`utterance_id` или `null`, `at: now | segment_end`), отвечает на `tts.synthesize` (`text`, `emotion`, `format: wav | ogg` → `path`, `duration_ms`; файлы в `%TEMP%\glados_voice\`), шлёт события `voice.speech` (`phase: started | segment | ended`, `utterance_id`, `seq`, `segment`, `completed`, `spoken_upto`, `reason`), `voice.utterance`, `voice.user_speaking`, `voice.heard`. Настройки: `barge_in`, `echo_tail_ms`. `GITS_VOICE_FAKE=1` — без звука.
  Распознавание речи (`stt_client.py`, `local_stt.py`): всегда локально на процессоре. Основной движок GigaAM v3 (`GITS_STT_ENGINE=gigaam`,
  по умолчанию, модель `GITS_STT_GIGAAM`, только русский), запасной faster-whisper int8 (модель `GITS_STT_MODEL`, по умолчанию small;
  включается сам, если GigaAM не открылся, или явно `GITS_STT_ENGINE=whisper`); язык `GITS_STT_LANGUAGE`, потоков не больше
  `GITS_STT_THREADS`, по умолчанию 2. Модель одна на процесс: её делят микрофон и слух (`hearing.py`). Веса только из кэша
  (`local_files_only`): если их нет, участник шины этого честно пишет в состояние и журнал и НЕ скачивает их сам. Диаризации и узнавания
  голосов нет: реплика с микрофона всегда от владельца (`voice.utterance`: `speaker = {"id": "owner", "name": null, "owner": true}`,
  `sender.is_owner = true`). Перебивание (`barge_in`) решает по тому, есть ли в куске речи настоящий текст, а не по голосу:
  надёжно в наушниках, на колонках эхо даёт ложные срабатывания. Эхо режется закрытием «ворот» микрофона. Состояние и панель
  «Голос» показывают режим («локально (CPU)» или «модель не загружена»); смена состояния логируется один раз.
- **`pet`** (ТЗ 13): capabilities `presence.view`, подписка на `presence`, `settings`, `module`; инструментов и событий нет. Окно в главном потоке, клиент в отдельном (`queue.Queue`); свои изменения настроек (глушилка, позиция, видимость, закрепление) отправляет через `POST /api/settings` дашборда. `GITS_PET_HEADLESS=1` — без окна.

## Тяжёлые участники

Тяжёлые окружения живут в `workspaces/<id>` со своими зависимостями и своей проверкой: `rimworld` (игра RimWorld, ТЗ 11) и `montage` (ТЗ 20). Запись в
`agent/peers.yaml` ссылается на папку через `{GITS}/workspaces/<id>`,
интерпретатор общий (`{PYTHON}`), если зависимостей участника шины хватает в нём. Такой участник шины хранит данные в своей папке, а не в
`MEMORY_DIR` ядра (супервизор передаёт `MEMORY_DIR` всем, участник шины RimWorld заменяет его своим `resources/<Имя>/memory`). Проверка у него своя:
`.\check.ps1` в папке участника шины, а тесты против `FakeCore` из этого SDK. Пример манифеста с `capabilities: ["game"]`, инструментом `order`
(приказ игроку-агенту: ядро зовёт его у участников шины фокуса с этой возможностью), `graph: true` (подграф профиля) и событиями: выше и в
`test/test_rimworld_core.py`.

## Проверка

Из корня репозитория `.\check.ps1` запускает ruff и тесты ядра, SDK и участников шины. Только SDK: `python -m pytest -q agent/interfaces/workspace/tests`; участник шины Telegram: `python -m pytest -q workspaces/telegram/tests`; голос: `python -m pytest -q services/voice/tests`; питомец: `python -m pytest -q services/pet/tests` (тесты каждого участника шины идут отдельным запуском).
