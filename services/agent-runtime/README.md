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

## Быстрая модель: `llm.mjs`

Отдельный долгоживущий процесс «один вопрос, один ответ», без агента, инструментов и истории (pi-ai напрямую). Один на ядро
(клиент `agent/pi_llm.py`), запросы идут параллельно. Протокол тот же: строки JSON.

| Ядро -> процесс | Смысл |
|---|---|
| `init` (`provider`: `openai` / `faux`, `auth_file`, для `faux`: `faux_responses`, `faux_tps`) | ответ `ready` или фатальная `error` («нет входа») |
| `complete` (`id`, `model`, `thinking`, `system`, `user`, `image_b64` или `null`, `image_mime`, `stream`) | вопрос; картинка уходит блоком изображения |
| `cancel` (`id`) | оборвать запрос: итог `failed` с `error: "cancelled"` |
| `shutdown` / EOF | выйти с кодом 0 |

Процесс -> ядро: `delta` (`id`, `text`; только при `stream: true`), затем ровно один `done` (`id`, `text` целиком,
`usage: {input, output}`) или `failed` (`id`, `error`). Сбой одного запроса не роняет процесс. Шаги `faux_responses` для
тестов: `{"text"}`, `{"error"}` (сбой поставщика), `{"echo": true}` (ответ описывает полученный запрос: текст, блоки,
системная подсказка).

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
