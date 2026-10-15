# 01. Контракт модуля: протокол, манифест, SDK

Этап 1. Репозиторий: `GitS`. Ветка: `tz/01-module-contract`.
Читать перед этим: `00-architecture.md`.

## 1. Цель

Один протокол, по которому любой модуль подключается к ядру: объявляет инструменты и события, получает вызовы,
шлёт события, речь и вопросы, ведёт долгие задачи. Плюс Python-пакет `gits_sdk` (SDK), чтобы модуль писался в
несколько строк, и проверочные тесты протокола.

Ядро-часть протокола (приём подключений, реестр) делается в ТЗ 02; здесь — спецификация, SDK и общие типы, которые
использует и ядро.

## 2. Объём

Входит:
- спецификация протокола (этот документ — источник правды; ядро и SDK обязаны ей соответствовать);
- пакет `GitS/sdk/gits_sdk/` (клиент модуля) и общие типы сообщений, которые импортирует ядро;
- тестовый «эхо-модуль» `GitS/sdk/examples/echo_module.py`;
- тесты соответствия протоколу.

Не входит: перенос реальных модулей (ТЗ 10–14), сервер ядра (ТЗ 02), внимание (ТЗ 03).

## 3. Транспорт

- **MOD-1 (ОБЯЗАТЕЛЬНО).** Транспорт — WebSocket, текстовые кадры, в каждом кадре один JSON-объект в UTF-8.
  Адрес ядра: `ws://127.0.0.1:8766/module/ws`. Переопределяется переменной окружения `GITS_CORE_URL`.
- **MOD-2 (ОБЯЗАТЕЛЬНО).** Подключается модуль (клиент), ядро — сервер. Модули не открывают своих портов для связи
  с ядром.
- **MOD-3 (ОБЯЗАТЕЛЬНО).** Только петлевой адрес: ядро принимает подключения лишь с `127.0.0.1` и `::1`, остальные
  отклоняет до рукопожатия (HTTP 403).
- **MOD-4 (ОБЯЗАТЕЛЬНО).** Аутентификация: заголовок `Authorization: Bearer <token>`. Токен — 32 случайных байта в hex,
  ядро генерирует при каждом старте (`secrets.token_hex(32)`) и пишет в файл
  `%LOCALAPPDATA%\GLaDOS\module_token` (каталог создаётся; права по умолчанию пользователя). Супервизор ядра передаёт
  токен запускаемым модулям в переменной `GITS_MODULE_TOKEN`. SDK берёт токен из `GITS_MODULE_TOKEN`, а если её нет —
  читает файл (для ручного запуска модуля при разработке). Неверный токен — HTTP 401 до рукопожатия. Токен никогда не
  пишется в журналы.
- **MOD-5 (ОБЯЗАТЕЛЬНО).** Пульс: обе стороны используют встроенный пинг WebSocket (`aiohttp`: `heartbeat=10`).
  Нет ответа 30 секунд — соединение считается разорванным.
- **MOD-6 (ОБЯЗАТЕЛЬНО).** Размер одного сообщения — не больше 1 МБ. Файлы (аудио, видео, картинки) не передаются
  внутри сообщений: передаются абсолютные пути на локальном диске.
- **MOD-7 (ОБЯЗАТЕЛЬНО).** Переподключение: при обрыве SDK переподключается с задержкой 1, 2, 4, 8, 16, 30, 30, ...
  секунд, бесконечно, пока процесс модуля жив. После переподключения модуль заново шлёт `hello`.

## 4. Конверт сообщения

Каждое сообщение:

```json
{
  "v": 1,
  "type": "event",
  "id": "m-3f9c2a7e5b1d4c08",
  "re": null,
  "ts": 1760012345.678,
  "body": {}
}
```

| Поле | Тип | Смысл |
|---|---|---|
| `v` | int | версия протокола, сейчас `1` |
| `type` | string | тип сообщения (таблицы ниже) |
| `id` | string | уникальный id отправителя: `m-` + 16 hex (`secrets.token_hex(8)`) |
| `re` | string \| null | для ответа — `id` запроса; иначе `null` |
| `ts` | float | `time.time()` отправителя |
| `body` | object | содержимое по типу |

- **MOD-8 (ОБЯЗАТЕЛЬНО).** Запрос и ответ. Сообщения, помеченные в таблицах как «запрос», ждут ответа типа `result`
  с `re = id запроса`:

```json
{"v": 1, "type": "result", "id": "m-...", "re": "m-<id запроса>", "ts": 0, "body": {"ok": true, "value": {}}}
{"v": 1, "type": "result", "id": "m-...", "re": "m-<id запроса>", "ts": 0,
 "body": {"ok": false, "error": {"code": "timeout", "message": "человекочитаемая причина"}}}
```

- **MOD-9 (ОБЯЗАТЕЛЬНО).** Коды ошибок (`error.code`), других не бывает:
  `bad_request` (неверное тело или аргументы), `unknown` (нет такого инструмента, задачи, типа), `denied` (пользователь
  не разрешил или политика запрещает), `unavailable` (модуль или ресурс сейчас недоступен), `timeout`, `cancelled`,
  `internal` (сбой кода). `message` — по-русски, без секретов и путей к секретам.
- **MOD-10 (ОБЯЗАТЕЛЬНО).** Неизвестный `type` получатель игнорирует и пишет предупреждение в журнал (не рвёт
  соединение). Если неизвестный тип был запросом — отвечает `result` с `unknown`.
- **MOD-11 (ОБЯЗАТЕЛЬНО).** Неизвестные поля в `body` игнорируются (совместимость вперёд).

## 5. Рукопожатие

1. Модуль открывает WebSocket с заголовком авторизации.
2. Модуль первым сообщением шлёт `hello`:

```json
{"v": 1, "type": "hello", "id": "m-...", "re": null, "ts": 0, "body": {
  "module_id": "rimworld",
  "instance": "c0ffee12",
  "pid": 12345,
  "protocol": 1,
  "manifest": { "...": "см. раздел 6" }
}}
```

`instance` — случайный id процесса (`secrets.token_hex(4)`), меняется при перезапуске процесса, не при переподключении.

3. Ядро отвечает `welcome` (с `re = id hello`) или `reject`:

```json
{"v": 1, "type": "welcome", "id": "m-...", "re": "m-<hello>", "ts": 0, "body": {
  "protocol": 1,
  "core_version": "2026.10.0",
  "enabled": true,
  "settings": {"streamer": false, "dnd": false},
  "module_settings": {"poll_seconds": 5},
  "profile": {"id": "rimworld", "title": "RimWorld", "focus_modules": ["rimworld"]},
  "battery": {"percent": 74, "sleeping": false}
}}
{"v": 1, "type": "reject", "id": "m-...", "re": "m-<hello>", "ts": 0, "body": {"reason": "protocol 2 не поддерживается"}}
```

- **MOD-12 (ОБЯЗАТЕЛЬНО).** До `welcome` модуль не шлёт ничего, кроме `hello`. После `reject` ядро закрывает
  соединение; SDK не переподключается при `reject` с причиной про протокол или манифест, а завершает процесс с кодом 2
  и понятной записью в журнал.
- **MOD-13 (ОБЯЗАТЕЛЬНО).** Если модуль с тем же `module_id` уже подключён, новое подключение вытесняет старое: ядро
  закрывает старое соединение (код закрытия 4000, причина `replaced`). Старый процесс SDK при коде 4000 не
  переподключается и завершается.
- **MOD-14 (ОБЯЗАТЕЛЬНО).** `enabled: false` в `welcome` означает: пользователь выключил модуль, но процесс запущен
  вручную. Модуль остаётся подключённым, событий и речи не шлёт, вызовы инструментов ядро ему не передаёт. Смена
  включённости приходит темой `module` (раздел 8).

## 6. Манифест

```json
{
  "id": "rimworld",
  "title": "RimWorld",
  "description": "Игра-симулятор колонии: колонисты, строительство, исследования, бой.",
  "version": "1.0.0",
  "capabilities": ["game"],
  "subscribe": ["presence", "settings", "profile", "module"],
  "shared_settings": ["streamer", "dnd", "quiet_until", "game_model", "claude_usage", "voice_muted"],
  "settings_schema": {
    "poll_seconds": {"type": "integer", "title": "Опрос игры, секунд", "default": 5, "min": 1, "max": 60}
  },
  "tools": [
    {
      "name": "order",
      "description": "Передать игроку-агенту приказ по игре своими словами пользователя.",
      "params": {"text": {"type": "string", "description": "приказ"}},
      "risk": "auto",
      "availability": "profile",
      "informative": false,
      "timeout_s": 15,
      "keywords": ["игра", "колония", "колонист", "римворлд"],
      "danger_check": false,
      "job": false
    }
  ],
  "emits": [
    {"kind": "rimworld.raid", "description": "Начался набег на колонию", "default_urgency": 5},
    {"kind": "rimworld.letter", "description": "Игровое письмо", "default_urgency": 2}
  ],
  "knowledge": [
    {"collection": "rimworld.wiki", "title": "Вики RimWorld", "scope": "rimworld", "kind": "reference"}
  ],
  "graph": true,
  "state": true
}
```

- **MOD-15 (ОБЯЗАТЕЛЬНО).** Поля манифеста:

| Поле | Обяз. | Тип | Смысл |
|---|---|---|---|
| `id` | да | string `^[a-z][a-z0-9_]{1,31}$` | совпадает с `hello.module_id` |
| `title` | да | string | название для дашборда и для планировщика |
| `description` | да | string ≤ 300 | что модуль даёт; читает планировщик |
| `version` | да | string semver | версия модуля |
| `capabilities` | нет | string[] | особые роли (раздел 6.2) |
| `subscribe` | нет | string[] | темы ядра, которые модуль хочет получать (раздел 8) |
| `shared_settings` | нет | string[] | какие общие настройки ядра нужны модулю (ключи `settings.DEFAULTS` ядра) |
| `settings_schema` | нет | object | собственные настройки модуля (раздел 6.3) |
| `tools` | нет | Tool[] | инструменты (раздел 6.1) |
| `emits` | нет | Emit[] | какие события шлёт: `kind`, `description`, `default_urgency` 0–5 |
| `knowledge` | нет | Collection[] | коллекции справочника, которыми владеет модуль (ТЗ 04) |
| `graph` | нет | bool | модуль отвечает на запрос `graph.get` (подграф для дашборда) |
| `state` | нет | bool | модуль шлёт сообщения `state` (сводка для дашборда и выбора профиля) |

- **MOD-16 (ОБЯЗАТЕЛЬНО).** Ядро проверяет манифест при `hello` (схема выше). Ошибка схемы — `reject` с перечнем
  проблем. `kind` в `emits` обязан начинаться с `<id>.`.

### 6.1. Инструмент

| Поле | Обяз. | Тип | Смысл |
|---|---|---|---|
| `name` | да | string `^[a-z][a-z0-9_]{1,47}$` | имя; полный id `<module>.<name>` |
| `description` | да | string ≤ 300 | для каких просьб инструмент. Первое предложение — главное |
| `params` | да | object | упрощённая схема параметров, как у `environments.base.Tool.params`: имя → `{"type": string\|integer\|number\|boolean\|array\|object, "description", "required" (по умолчанию true), "enum"}` |
| `risk` | да | `auto` \| `ask` \| `public` | политика разрешений (как в `environments/base.py`) |
| `availability` | да | `always` \| `profile` | `always` — виден в любом профиле; `profile` — только когда модуль в фокусе активного профиля |
| `informative` | нет (false) | bool | результат нужно сообщить пользователю |
| `self_confirming` | нет (false) | bool | инструмент сам спрашивает подтверждение (через `core.ask`), ядро не спрашивает второй раз |
| `timeout_s` | нет (30) | int 1–120 | сколько ядро ждёт `result` на `tool.call` |
| `keywords` | нет | string[] | слова для отбора инструментов |
| `danger_check` | нет (false) | bool | перед вызовом ядро шлёт `tool.check`; если модуль вернёт причину, разрешение спрашивается всегда |
| `job` | нет (false) | bool | вызов запускает задачу: модуль отвечает сразу `{"job_id": ...}`, дальше `job.update` |
| `fallback` | нет (false) | bool | запасной универсальный инструмент: попадает к планировщику при любом отборе (`Tool.always` в ядре). Поле есть в схеме с этапа 1 (SDK его принимает); ядро учитывает его с ТЗ 14 |
| `announce` | нет (false) | bool | долгое или заметное действие на ПК: перед вызовом ядро озвучивает факт «запускаю <name>: <описание и аргументы>» (через внимание, `urgency_hint 3`); быстрые инструменты — false (README, правило 14) |
| `core_fills` | нет | string[] | параметры, которые заполняет ядро, а не планировщик (планировщик их не видит). Поддерживается только `"context"`: краткий контекст разговора без услышанного (`companion._context(include_hearing=False, query=<первый строковый параметр>)`), до 4000 символов. Поле есть в схеме с этапа 1 (SDK его принимает); ядро заполняет его с ТЗ 14 |

- **MOD-17 (ОБЯЗАТЕЛЬНО).** Синхронный инструмент (`job: false`) обязан ответить за `timeout_s`. Если работа дольше
  120 секунд, это задача (`job: true`).

### 6.2. Возможности (`capabilities`)

| Значение | Кто | Что значит |
|---|---|---|
| `voice.output` | `voice` | принимает `speak` и `speak.stop`, отвечает на `tts.synthesize` |
| `voice.input` | `voice` | шлёт события `voice.utterance` |
| `presence.view` | `pet` | показывает присутствие (ядру ничего не нужно, кроме подписки на `presence`) |
| `screen.observe` | `desktop` | шлёт события об активном окне; в `state` есть `foreground_app` |
| `chat.channel` | `telegram` | принимает `deliver` (доставка ответа в свой канал) |
| `game` | `rimworld` | игровой модуль; в `state` есть `game_running` |

- **MOD-18 (ОБЯЗАТЕЛЬНО).** `voice.output` одновременно может быть только у одного подключённого модуля. Второй
  получает `reject` с причиной.

### 6.3. Собственные настройки модуля

`settings_schema`: имя → `{"type": boolean|integer|number|string, "title", "default", "min"?, "max"?, "enum"?,
"secret"?: false}`. Ядро хранит значения в своём `settings.json` под ключом `modules.<id>.<имя>`, рисует форму в
дашборде автоматически и шлёт текущие значения в `welcome.module_settings` и в теме `settings` (поле
`module_settings`). Поле с `secret: true` запрещено (секреты модули читают сами из `secrets.env`, см. ТЗ 10).

## 7. Сообщения модуль → ядро

| type | Запрос? | body | Смысл |
|---|---|---|---|
| `hello` | нет | раздел 5 | рукопожатие |
| `event` | нет | раздел 7.1 | что-то произошло; идёт в менеджер внимания |
| `say` | нет | раздел 7.2 | модуль хочет, чтобы она что-то сказала |
| `emotion` | нет | `{"emotion": str, "cause": str}` | эмоция (из списка `fast_brain.EMOTIONS`) |
| `state` | нет | `{"summary": str ≤ 300, "data": object}` | сводка для дашборда и выбора профиля; ядро хранит последнюю |
| `job.update` | нет | раздел 9 | ход задачи |
| `result` | — | раздел 4 | ответ на запрос ядра |
| `core.ask` | да | раздел 7.3 | вопрос пользователю |
| `core.tts` | да | `{"text": str, "emotion": str?, "format": "wav"\|"ogg"}` → `{"path": str, "duration_ms": int}` | синтез её голосом в файл (ядро передаёт в `voice`) |
| `core.tool.call` | да | `{"tool": "desktop.read_screen", "args": {}}` → `{"text": str}` | вызвать инструмент другого модуля или ядра с обычными разрешениями |
| `core.lookup` | да | ТЗ 04 | поиск по справочнику и интернету |
| `core.memory.recall` | да | ТЗ 04 | поиск в личной памяти |
| `core.memory.remember` | да | ТЗ 04 | записать факт в личную память |
| `core.experience.add` | да | ТЗ 04 | записать опыт |
| `core.kb.ingest` | да | ТЗ 04 | положить документы в свою коллекцию справочника |
| `core.persona` | да | `{}` → `{"name": str, "card": str ≤ 2000}` | карточка характера для модулей, которые сами генерируют текст |
| `core.privacy_terms` | да | `{}` → `{"terms": [str]}` | личные слова (имена, факты о владельце) для фильтров публикации; реализуется в ТЗ 10 (до этого — `unknown`) |
| `core.claude_usage` | да | `{"tokens": int, "source": str, "limit_text"?: str}` → `{}` | учесть расход Claude в батарейке; `limit_text` — текст исчерпанного лимита (ядро: `claude_quota.note`) |
| `core.web_search` | да | `{"query": str}` → `{"text": str}` | поиск в интернете (до ТЗ 04; после — синоним `core.lookup` с `allow_web`) |

### 7.1. Событие

```json
{
  "kind": "telegram.dm",
  "text": "Сообщение от Маши в личке: «ты скоро?»",
  "data": {"chat_id": 123, "message_id": 456},
  "urgency_hint": 3,
  "addressed_to_me": true,
  "sender": {"id": "tg:123", "name": "Маша", "is_owner": false},
  "reply_channel": {"module": "telegram", "target": "tg:123"},
  "dedupe_key": "telegram.dm:123:456",
  "expires_in_s": 900,
  "memorable": true
}
```

| Поле | Обяз. | Смысл |
|---|---|---|
| `kind` | да | `<module>.<что>`; должен быть объявлен в `emits` (иначе ядро пишет предупреждение, но принимает) |
| `text` | да | факт по-русски, ≤ 1000 символов: что произошло. Не реплика для озвучки |
| `data` | нет | машинные подробности |
| `urgency_hint` | нет | 0–5, оценка модуля; иначе берётся `default_urgency` из `emits` |
| `addressed_to_me` | нет | обращаются ли к ней (её имя, личка ей, вопрос ей) |
| `sender` | нет | кто: `id` (уникальный в модуле, с префиксом), `name`, `is_owner` |
| `reply_channel` | нет | куда отвечать: модуль с `chat.channel` и его адресат |
| `dedupe_key` | нет | одинаковые ключи в течение 60 с склеиваются |
| `expires_in_s` | нет | после этого событие устарело и не озвучивается |
| `memorable` | нет (false) | содержит устойчивые факты, стоит отдать в личную память |
| `announce` | нет (false) | объявление о долгом запуске (README, правило 14): идёт мимо формулы важности как `announce` (ТЗ 03, AN-1…AN-2). То же для любого события вида `<модуль>.launch` |

### 7.2. Речь

```json
{"text": "Колонист Борис снова уснул на посту.", "mode": "fact", "emotion": "angry",
 "urgency_hint": 2, "expires_in_s": 30, "key": "rimworld.comment"}
```

- `mode: "fact"` — `text` это факт; реплику по нему сформирует ядро в её характере.
- `announce: true` — объявление о долгом запуске: звучит всегда (приоритет 75, срок 180 с, без паузы между репликами), коротко
  при «не отвлекать», уходит в сводку, если пользователя нет (ТЗ 03, AN-1, AN-2).
- `mode: "verbatim"` — `text` уже реплика в её характере (модуль сгенерировал её моделью, например комментатор
  RimWorld). Ядро произносит как есть (после `speech_text.clean_speech`), если менеджер внимания разрешит.
- Речь всегда проходит через менеджер внимания (ТЗ 03): может быть отложена или отброшена. Модуль не получает
  подтверждения, что фраза сказана, — для этого есть тема `presence`.

### 7.3. Вопрос пользователю

```json
{"kind": "approval", "question": "Монтажёр хочет удалить черновые рендеры (3 файла, 1,2 ГБ).",
 "options": ["да", "нет"], "timeout_s": 600, "context": {"job_id": "j-7a1c"}, "urgency_hint": 3}
```

- `kind`: `approval` (да/нет; ответ `{"answer": true|false, "by": "user"|"timeout"}`), `choice` (один из `options`;
  ответ `{"answer": "<option>", "by": ...}`), `text` (свободный ответ; `{"answer": "<текст>", "by": ...}`).
- `question` — факт, о чём спросить. Формулировку вслух строит ядро в её характере.
- По тайм-ауту: `approval` → `false`, `choice` и `text` → `null`, `by: "timeout"`. Тайм-аут по умолчанию 300 с,
  максимум 86400.
- Ядро задаёт вопрос по правилам внимания (ТЗ 03) и в канал, где пользователь сейчас (голос, чат дашборда; в Telegram —
  если пользователя нет за компьютером больше 10 минут и включён `telegram`).

## 8. Сообщения ядро → модуль

| type | Запрос? | body | Смысл |
|---|---|---|---|
| `welcome` / `reject` | — | раздел 5 | рукопожатие |
| `tool.call` | да | `{"tool": "<name без модуля>", "args": {...}}` → любое JSON-значение в `value` | вызов инструмента; аргументы уже проверены по схеме |
| `tool.check` | да | `{"tool": str, "args": {}}` → `{"danger": str \| null}` | для `danger_check: true`; ответ ≤ 2 с |
| `job.answer` | нет | `{"job_id": str, "question_id": str, "answer": any}` | ответ пользователя на вопрос задачи (раздел 9) |
| `job.cancel` | да | `{"job_id": str}` → `{}` | отменить задачу |
| `job.list` | да | `{}` → `{"jobs": [JobStatus]}` | все незавершённые и последние 20 завершённых |
| `graph.get` | да | `{}` → `{"nodes": [...], "edges": [...]}` | подграф для дашборда (формат как сейчас у `:8767/api/graph`) |
| `deliver` | да | `{"target": str, "text": str, "voice_path": str?, "attachments": [str]?}` → `{"delivered": bool}` | доставить ответ в канал (`chat.channel`); `target` — из `reply_channel` события или особое значение `"owner"` (личный канал владельца в этом модуле) |
| `speak` | нет | ТЗ 12 | только для `voice.output` |
| `speak.stop` | нет | ТЗ 12 | только для `voice.output` |
| `tts.synthesize` | да | ТЗ 12 | только для `voice.output` |
| `topic` | нет | `{"topic": str, "data": object}` | широковещание по подписке |
| `shutdown` | нет | `{"reason": str}` | завершиться за 10 с; иначе супервизор убьёт процесс |

Темы (`topic`), на которые модуль подписывается полем `subscribe`:

| topic | data | Когда |
|---|---|---|
| `presence` | `{"speaking": bool, "utterance_id": str\|null, "segment": str, "emotion": str, "emotion_ts": float, "busy": bool, "focus": str, "user_speaking": bool, "last_user_ts": float, "last_say": str, "last_say_ts": float}` | при каждом изменении, не чаще 10 раз в секунду |
| `settings` | `{"settings": {только shared_settings модуля}, "module_settings": {...}}` | при изменении |
| `profile` | `{"id": str, "title": str, "focus_modules": [str]}` | при смене профиля |
| `module` | `{"enabled": bool}` | при включении или выключении этого модуля |
| `battery` | `{"percent": int, "sleeping": bool}` | при изменении на 5 и более процентов или смене `sleeping` |
| `character` | `{"id": str, "name": str, "aliases": [str], "gender": str, "language": str, "world": str, "voice": {...}}` | при переключении персонажа (ТЗ 05, CHR-6); тексты персоны (разделы `persona.md`) модуль берёт запросом `core.persona` |

## 9. Задачи

Состояния: `queued` → `running` ⇄ `waiting_user` → `done` | `failed` | `cancelled`.

`job.update` (модуль → ядро):

```json
{"job_id": "j-7a1c", "title": "Шортс: эксперимент с кубом", "state": "waiting_user", "stage": "plan_approval",
 "progress": 0.35, "text": "План монтажа готов: 6 сцен, 48 секунд.",
 "question": {"question_id": "q-1", "kind": "choice", "question": "Утвердить план монтажа?",
              "options": ["утвердить", "переделать"], "timeout_s": 86400},
 "artifacts": [{"path": "C:\\MyProjects\\montage-module\\jobs\\j-7a1c\\plan\\plan.md", "kind": "plan"}],
 "error": null}
```

- **MOD-19 (ОБЯЗАТЕЛЬНО).** `job_id` = `j-` + 8 hex, уникален в модуле. Модуль шлёт `job.update` при каждой смене
  `state` или `stage` и не чаще раза в 2 секунды для прогресса.
- **MOD-20 (ОБЯЗАТЕЛЬНО).** В состоянии `waiting_user` обязателен `question` (те же поля, что у `core.ask`, плюс
  `question_id`). Ядро задаёт вопрос по правилам внимания и отвечает `job.answer`. Повторный `job.update` с тем же
  `question_id` не порождает второй вопрос.
- **MOD-21 (ОБЯЗАТЕЛЬНО).** `failed` — с `error` (`{"code", "message"}` из MOD-9). `text` во всех состояниях — факт для
  её реплики, не готовая реплика.
- **MOD-22 (ОБЯЗАТЕЛЬНО).** После переподключения модуль заново шлёт `job.update` по всем незавершённым задачам.

## 10. SDK `gits_sdk`

Расположение: `GitS/sdk/` — устанавливаемый пакет (`pyproject.toml`, имя дистрибутива `gits-sdk`, импорт
`gits_sdk`, Python ≥ 3.12, зависимость только `aiohttp`). Установка в окружение модуля:
`pip install -e C:\MyProjects\GitS\sdk`.

Состав:
- `gits_sdk/protocol.py` — константы (`PROTOCOL = 1`, коды ошибок, типы), dataclass-ы конверта, манифеста,
  инструмента, события, задачи; валидация манифеста (`validate_manifest(dict) -> list[str]` — список проблем).
  **Этот же файл импортирует ядро** (не копирует): ядро добавляет `GitS/sdk` в зависимости своего окружения.
- `gits_sdk/client.py` — класс `ModuleClient`.
- `gits_sdk/testing.py` — `FakeCore`: поддельное ядро в памяти для тестов модулей (принимает `hello`, отвечает
  `welcome`, записывает все сообщения, позволяет вызвать инструмент и ответить на запрос).

API `ModuleClient` (имена фиксированы):

```python
from gits_sdk import ModuleClient, ToolError

client = ModuleClient(manifest: dict, *, url: str | None = None, token: str | None = None)

@client.tool("order")                      # имя из манифеста; функция async (args: dict) -> JSON-значение
async def order(args): ...                 # ToolError("причина", code="unavailable") -> result ok:false

@client.check("delete_files")              # обработчик tool.check: async (args) -> str | None
@client.on_topic("presence")               # async (data: dict) -> None
@client.on_job_answer                      # async (job_id, question_id, answer) -> None
@client.on_job_cancel                      # async (job_id) -> None
@client.on_request("speak")                # прочие типы ядро->модуль (для voice: speak, speak.stop, tts.synthesize)

await client.event(kind, text, **fields)   # MOD 7.1
await client.say(text, mode="fact", **fields)
await client.emotion(emotion, cause)
await client.state(summary, data)
await client.job_update(job_id, **fields)
answer = await client.ask(question, kind="approval", options=None, timeout_s=300, context=None)
value = await client.request("core.tts", {"text": "...", "format": "wav"}, timeout=60)  # общий запрос к ядру
client.settings        # dict: текущие shared_settings
client.module_settings # dict
client.enabled         # bool
client.profile         # dict
await client.run()     # подключение, рукопожатие, цикл; не возвращается, пока не shutdown/replaced
```

- **MOD-23 (ОБЯЗАТЕЛЬНО).** SDK сам: валидирует манифест до подключения; проверяет, что для каждого инструмента
  манифеста зарегистрирован обработчик (иначе исключение при `run()`); переподключается (MOD-7); после
  переподключения вызывает пользовательский хук `on_reconnect` (если задан), чтобы модуль переслал задачи (MOD-22);
  не даёт слать `event`/`say` при `enabled == False` (тихо отбрасывает с записью debug в журнал); ограничивает
  `job_update` по прогрессу (MOD-19).
- **MOD-24 (ОБЯЗАТЕЛЬНО).** Обработчики инструментов выполняются как отдельные задачи asyncio: долгий вызов не
  блокирует приём сообщений. Исключение, отличное от `ToolError`, превращается в `internal` с текстом исключения
  (без трассировки) и пишется в журнал модуля с трассировкой.
- **MOD-25 (ОБЯЗАТЕЛЬНО).** `client.request` и `client.ask` бросают `ToolError(code=...)` при `ok: false` и
  `asyncio.TimeoutError` при тайм-ауте ожидания. Тайм-аут `ask` = `timeout_s + 30`.
- **MOD-26 (ЖЕЛАТЕЛЬНО).** Журналы SDK — через `logging.getLogger("gits_sdk")`.

## 11. Эхо-модуль и тесты

- **MOD-27 (ОБЯЗАТЕЛЬНО).** `GitS/sdk/examples/echo_module.py`: модуль `echo` с инструментом `echo.repeat`
  (`text` → тот же текст, `availability: always`, `risk: auto`) и задачей `echo.count` (`job: true`, `n` — считает до
  n раз в секунду, шлёт прогресс, на середине спрашивает `choice` «продолжать?»). Используется в тестах ядра (ТЗ 02) и
  как пример для авторов модулей.
- **MOD-28 (ОБЯЗАТЕЛЬНО).** Тесты `GitS/sdk/tests/`: валидация манифеста (верные и неверные примеры для каждого поля
  из MOD-15/6.1); конверт и `result`; `ModuleClient` против `FakeCore`: рукопожатие, вызов инструмента, `ToolError`,
  исключение → `internal`, `ask` с ответом и с тайм-аутом, переподключение (FakeCore рвёт соединение — SDK
  возвращается и шлёт `hello`), код 4000 → SDK завершается, `enabled: false` → события отбрасываются.
  Подключение в тестах — к настоящему aiohttp-серверу FakeCore на свободном порту `127.0.0.1`.
- **MOD-29 (ОБЯЗАТЕЛЬНО).** `GitS/check.ps1` прогоняет и тесты `sdk/tests`.

## 12. Документация

- **MOD-30 (ОБЯЗАТЕЛЬНО).** `GitS/docs/MODULES.md` — короткое руководство автора модуля: как подключиться (5 шагов),
  пример манифеста, пример модуля на 30 строк (эхо), правила (факты а не реплики, разрешения через ядро, файлы путями,
  секреты сами из `secrets.env`). Ссылка на этот документ как на полную спецификацию.

## 13. Приёмка

1. `pip install -e sdk` в окружении GitS проходит; `python -c "import gits_sdk"` работает.
2. Все тесты MOD-28 зелёные, `.\check.ps1` зелёный.
3. Эхо-модуль, запущенный против `FakeCore`, проходит сценарий: подключение → `repeat` → `count` с вопросом → ответ →
   `done`.
4. `docs/MODULES.md` есть.

## 14. Дополнение ТЗ 18: политики фокуса (протокол и манифест)

Добавлено 2026-10-13 (ТЗ 18, FOC-1…FOC-4). Старые модули продолжают работать без изменений.

- **MOD-31 (ОБЯЗАТЕЛЬНО, если объявлено `policies`).** Манифест может содержать `policies` (список политик) и
  `default_policy` (id фоновой политики). Формат и проверки: `docs/MODULES.md`, раздел «Политики фокуса»; `validate_manifest`
  (`sdk/gits_sdk/protocol.py`, `_check_policies`) проверяет: минимум одна политика с `focus: background`,
  `default_policy` - фоновая, `transitions` ссылаются на существующие политики. Без `policies` фокуса у модуля нет.
- **MOD-32 (ОБЯЗАТЕЛЬНО).** Новые запросы ядра модулю (тип `request`, модуль отвечает `result`): `policy.activate`
  `{policy, goal, task_id, event_id, priority, reason}` и `focus.release` `{reason, task_id, force, event_id, checkpoint}`,
  `focus.resume` `{task_id, policy, goal, checkpoint, subgoals}`. Ответ: `result` с `value.ok`. Отказ модуля (`ok: false`
  или `denied`) оставляет фокус у прежнего владельца; `unknown` на `focus.release` означает, что модуль не умеет освобождать
  фокус. Протокол переключения и правила - `docs/FOCUS.md`.
- **MOD-33 (ОБЯЗАТЕЛЬНО).** Статус модуля для ядра: тема `state`, поле `data.status` с полями `active_policy`,
  `interruptible`, `blocked` (строка при блокировке), `active_goal`, `urgency`, `estimated_remaining_s` (для дашборда).
  Ядро не верит статусу в первые 5 с после перехода фокуса (GRACE_S).
- **MOD-34 (ОБЯЗАТЕЛЬНО).** Решения внимания (D2 и выше) и критические события (срочность 5) по модулям с `policies`
  попадают в очередь фокуса (`agent/focus.py`). Событие модуля без `policies` в очередь фокуса не попадает.
- Поле `quiet: true` в политике (расширение, в исходном ТЗ 18 его нет): в такой политике обычные события не переключают
  фокус (режим «не беспокоить» модуля, например Telegram `do_not_disturb`).
