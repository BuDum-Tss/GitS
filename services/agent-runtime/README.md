# agent-runtime

Мост между ядром (Python) и `@earendil-works/pi-agent-core` (Node). Ядро запускает `node bridge.mjs` и говорит с ним
строками JSON: stdin от ядра, stdout к ядру, журнал только в stderr. Инструменты исполняет ядро: мост шлёт `tool_call`
и ждёт `tool_result`. Поставщики: `openai` (вход по подписке ChatGPT) и `faux` (поддельный, для тестов).

## Протокол

Ядро -> мост:

| сообщение | смысл |
|---|---|
| `init` (`provider`, `model`, `thinking`, `system`, `tools`, `auth_file`, `session_file`, `session_id`, `max_context_chars`, `faux_responses`) | настройка; ответ `ready` (`restored`) или фатальный `error` |
| `prompt` (`id`, `text`) | начать ход; если ход идёт: `error` `busy` |
| `steer` (`text`) | подмешать сообщение в идущий ход; хода нет: `error` `idle` |
| `follow_up` (`text`) | добавить после текущего дела; хода нет: `error` `idle` |
| `abort` | оборвать ход; ждущие инструменты отклоняются сразу |
| `tool_result` (`call_id`, `ok`, `text`) | итог инструмента; `ok: false` модель увидит как ошибку |
| `set_system` (`system`) | новая системная подсказка, история остаётся (вне хода) |
| `reset` | забыть историю и удалить `session_file`; ответ `reset_done` |
| `shutdown` | сохранить историю и выйти с кодом 0 (EOF на stdin то же самое) |

Мост -> ядро: `ready`, `reset_done`, `text_delta` (кусок текста), `text` (целое сообщение модели), `tool_call`
(`call_id`, `name`, `args`), `end` (`status`: `ok`, `aborted`, `error`; `error`; `usage`), `error` (`message`, `fatal`).
Каждый принятый `prompt` заканчивается ровно одним `end`.

История пишется в `session_file` после каждого `end` (временный файл и переименование). Если она длиннее
`max_context_chars`, самое старое заменяется пометкой «[ранняя часть разговора опущена]» (граница только по сообщению
`user`).

## Установка и вход

```
npm install
npx @earendil-works/pi-ai login openai
```

Вход создаёт `auth.json` в этой папке (в git не попадает; ядро берёт путь из `GITS_PI_AUTH`, иначе этот файл).
Без входа мост отвечает фатальной ошибкой «нет входа».

## Тесты

```
npm test
```

Тесты идут на поддельном поставщике, без сети и без входа.

## Рабочий агент (`worker.mjs`): файлы и команды

Замена Claude Code там, где нужны встроенные инструменты: `worker.mjs` работает поверх `@earendil-works/pi-coding-agent`
(`createAgentSession`). Клиент для ядра и модулей: `sdk/gits_sdk/pi_agent.py` (`PiAgent`). Протокол строк JSON, как у моста.

`init`: `provider` (`openai` | `faux`), `model`, `thinking`, `auth_file`, `cwd` (рабочая папка встроенных инструментов), `system` или
`system_file`, `tools` (встроенные: `read`, `grep`, `find`, `ls`, `bash`, `edit`, `write`), `custom_tools` (свои, исполняет
вызывающая сторона: `tool_call` / `tool_result`), `skills_dir` (или `null`), `session_file` (история между запусками или `null`),
`max_turns`, `faux_responses`. Ответ `ready` (`restored`, `tools`) или фатальный `error`.

Дальше: `prompt` (`id`, `text`), `steer`, `abort`, `shutdown`. События: `text_delta`, `text`, `tool_call` (свой инструмент),
`permission`, `tool_use` (после разрешения, для журнала), `end` (`status`: `ok` | `aborted` | `error` | `max_turns`, `error`,
`usage` {`requests`, `input`, `output`}, `turns`).

### Разрешения (главное)

Каждый вызов встроенного инструмента до исполнения уходит вызывающей стороне: `{"type":"permission","call_id","tool","input"}`.
Исполняется он только после `{"type":"permission_result","call_id","allow":true}`; при `allow:false` (или любом другом значении)
модель видит `reason`. Без ответа инструмент не исполняется никогда; `abort` и `max_turns` закрывают ждущие вызовы отказом.
Реализовано расширением pi на событии `tool_call` (ошибка расширения тоже блокирует). Инструменты вне `tools` модели не объявляются.
Свои инструменты (`custom_tools`) разрешений не спрашивают: их исполняет сама вызывающая сторона. В `PiAgent` это `can_use_tool(tool, input) -> (allow, reason)`;
нет функции или она упала: отказ.

### Изоляция от домашней папки

Служебная папка pi (`PI_CODING_AGENT_DIR`) временная и пустая; расширения, шаблоны, темы и контекстные файлы (`AGENTS.md`,
`CLAUDE.md`) не загружаются, настройки в памяти, скиллы только из `skills_dir`, вход только из `auth_file`. Из окружения процесса
удаляются переменные `*_API_KEY`, `*_TOKEN`, `*_SECRET` (клиент вдобавок не передаёт процессу переменные с KEY, TOKEN, SECRET,
PASSWORD в имени: `bash` наследует окружение). Папка `cwd` не песочница: путь `../` или абсолютный решает `can_use_tool`.

### Соответствие инструментов pi и Claude (`gits_sdk.pi_agent.to_claude`)

| pi | Claude | поля |
|---|---|---|
| `read` | `Read` | `path` -> `file_path` (абсолютный относительно `cwd`), `offset`, `limit` |
| `write` | `Write` | `path` -> `file_path`, `content` |
| `edit` | `Edit` | `path` -> `file_path`; `edits[0]` -> `old_string`, `new_string` (весь список в `edits`) |
| `bash` | `Bash` | `command`, `timeout` |
| `grep` | `Grep` | `pattern`, `path` (абсолютный), `glob`, `ignoreCase` -> `-i`, `context` -> `-C`, `limit` -> `head_limit` |
| `find` | `Glob` | `pattern`, `path` (абсолютный) |
| `ls` | `LS` | `path` (абсолютный) |

Установка: `npm ci` в этой папке; вход `auth.json` общий с мостом. Где лежит мост: параметр `runtime_dir` или `GITS_PI_RUNTIME_DIR`.
