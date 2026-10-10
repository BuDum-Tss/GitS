# 11. `rimworld-module`: переименование и перевод на контракт

Этап 2. Репозитории: `rimworld-agent` → `rimworld-module` (основной), `GitS` (удаление старых каналов).
Ветки: `tz/11-rimworld-module` в обоих.
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`.

## 1. Цель

1. Переименовать репозиторий и папку `rimworld-agent` в `rimworld-module`.
2. Агент игры подключается к ядру как модуль `rimworld` по контракту 01 вместо `speech_link.py` (HTTP к :8766) и
   `game_api.py /api/order` (HTTP с :8767).
3. Инструменты RimWorld попадают в маршрутизатор ядра из манифеста модуля, а не через MCP-манифест GitS.
4. В GitS удаляются старые каналы игры (`game_bridge.py`, `/api/say`, `/api/speech_status`, `/api/emotion`,
   `/api/event`, `/api/tool`, `/api/web_search`, `/api/claude_usage`, `environments/manifests/rimworld.json`).

Игровое поведение (контуры хода, планирования, тактики, комментатор, мод, оверлей OBS) не меняется.

## 2. Предварительные условия (решает человек, до начала работы)

**Статус на 2026-10-10:** работа по записи видео закоммичена (`aeb11e7`) и вместе с веткой `agent-startup` влита в
`master` (`c9d7c81`, `22bfd42`; 111 тестов, мод собирается); переименование на GitHub через `gh repo rename`
владелец разрешил; рабочие копии — чинить (`git worktree repair`), не удалять. Пункты ниже — для истории.

Исполнитель проверяет эти пункты и, если хоть один не закрыт, останавливается и спрашивает человека:

- **Незакоммиченная работа по записи видео** лежит в рабочей копии
  `C:\MyProjects\rimworld-agent\.claude\worktrees\agent-video-recording-youtube-66c636` (ветка
  `claude/agent-video-recording-youtube-66c636`, изменения не закоммичены): `run_recorder.py`, `scenario.py`,
  `audio_capture.py`, `camera_director.py`, `game_watchdog.py`, `mod/AgentBridge/Source/Camera/CameraDirector.cs`,
  `CameraCommands.cs`, правки `controller.py`, `main.py`, `settings.py`, `game_launcher.py` и др. Человек решает:
  закоммитить и влить в `master` до этого ТЗ (рекомендуется) или отложить. Без решения переименование не делать:
  рабочие копии ссылаются на абсолютный путь папки.
- **Невлитые коммиты** в ветке `claude/agent-startup-068525` (`0d04c38`, `92de7c9`: режимы игры с наборами
  инструментов, локальные ходы на Gemma). Человек решает: влить или отказаться.
- **Рабочие копии** `C:\MyProjects\rimworld-agent\.claude\worktrees\*` (8 штук на 2026-10-09; список —
  `git worktree list`). После переименования папки их нужно починить (`git worktree repair`) или удалить. Человек
  решает, какие удалить.
- **GitHub.** Удалённый репозиторий `https://github.com/BuDum-Tss/rimworld-agent`. Переименование на GitHub — внешнее
  действие: только с разрешения человека (или человек делает сам в настройках репозитория). GitHub оставляет
  перенаправление со старого имени.
- **Процессы.** Перед переименованием папки игра, агент игры, ядро GitS и все сессии Claude Code в этой папке закрыты.

## 3. Переименование

- **RW-1 (ОБЯЗАТЕЛЬНО).** Порядок:
  1. все изменения закоммичены; `git status` чистый;
  2. (с разрешения) GitHub: переименовать репозиторий в `rimworld-module`; локально
     `git remote set-url origin https://github.com/BuDum-Tss/rimworld-module.git`;
  3. закрыть процессы (раздел 2), переименовать папку `C:\MyProjects\rimworld-agent` → `C:\MyProjects\rimworld-module`;
  4. `git worktree repair` из новой папки; проверить `git worktree list`;
  5. обновить ссылки на старое имя (RW-2);
  6. проверить запуск (раздел 11).
- **RW-2 (ОБЯЗАТЕЛЬНО).** Заменить упоминания пути и имени `rimworld-agent` на `rimworld-module` (найти:
  `rg -n "rimworld-agent|RIMWORLD_AGENT"` в обоих репозиториях, кроме `CHANGELOG.md` прошлых записей и `.git`):
  - `rimworld-module`: `agent/run_mcp.py` (жёсткий путь — заменить на путь относительно файла), `agent/game_api.py`,
    `agent/glados_agent.py`, `README.md`, `AGENTS.md`, `diagnose.ps1`, `rebuild_and_launch.ps1`. Файлы в
    `mod/AgentBridge/obj/` — сгенерированные: не править, пересоздадутся сборкой.
  - `GitS`: `tray.py`, `main.py`, `dashboard.py`, `README.md`, `AGENTS.md`, `secrets.env.example`, `modules.yaml`,
    документы `docs/tz/*` — не править (там старое имя упоминается намеренно в истории).
  - Переменная окружения: новая `RIMWORLD_MODULE_DIR`; старая `RIMWORLD_AGENT_DIR` читается как запасная одну версию
    (с предупреждением в журнал) и удаляется в следующем ТЗ, которое затронет эти файлы. В `secrets.env.example` —
    новая. Пользователю в отчёте: «в `agent/secrets.env` замени `RIMWORLD_AGENT_DIR` на `RIMWORLD_MODULE_DIR`»
    (исполнитель сам `secrets.env` не читает и не правит).
- **RW-3 (ЖЕЛАТЕЛЬНО).** Память Claude Code для проекта хранится по пути папки
  (`C:\Users\coffe\.claude\projects\C--MyProjects-rimworld-agent\memory\`). Предложить человеку скопировать её в
  `...\C--MyProjects-rimworld-module\memory\` (сделать только с разрешения).

## 4. Окружение модуля

- **RW-4 (ОБЯЗАТЕЛЬНО).** `rimworld-module` получает своё виртуальное окружение `.venv` в корне (если его ещё нет —
  создать, `pip install -r agent/requirements.txt -e C:\MyProjects\GitS\sdk`; установка пакетов — с разрешения
  человека). В `agent/requirements.txt` добавить строку-комментарий об SDK (путь к SDK задаётся при установке, не в
  файле). `README.md`: раздел «Установка» с этими командами.
- **RW-5 (ОБЯЗАТЕЛЬНО).** GitS `modules.yaml`:

```yaml
  - id: rimworld
    cwd: "{RIMWORLD_MODULE_DIR}/agent"
    command: "{RIMWORLD_MODULE_DIR}/.venv/Scripts/python.exe"
    args: ["-u", "main.py"]
    autostart: true
    restart: on-failure
```

## 5. Подключение к ядру

- **RW-6 (ОБЯЗАТЕЛЬНО).** Новый файл `agent/core_link.py` — замена `speech_link.py` с тем же внешним интерфейсом,
  чтобы контроллер и комментатор не переписывать:

| `SpeechLink` (было) | `CoreLink` (стало) |
|---|---|
| `online` | `client` подключён и `enabled` |
| `last_user_ts()` | из темы `presence.last_user_ts` |
| `is_speaking()` | `presence.speaking` |
| `repeats(text)` | та же логика, список недавних реплик — из `presence.last_say` (копить последние 10 в памяти модуля) |
| `say(text, emotion)` | `client.say(text, mode="verbatim", emotion=emotion, key="rimworld.comment", expires_in_s=20)` |
| `emotion(event, emotion)` | `client.emotion(emotion, cause=event)` |
| `event(kind, data)` | см. RW-9 |
| `run()` (опрос `/api/speech_status`) | `client.run()`; общие настройки — из темы `settings` → `settings.update(**shared)` как сейчас |

  `speech_link.py` удалить. `main.py`: создаёт `ModuleClient(manifest)`, `CoreLink(client)` вместо `SpeechLink`.
- **RW-7 (ОБЯЗАТЕЛЬНО).** `shared_settings`: `streamer`, `dnd`, `quiet_until`, `game_model`, `claude_usage`,
  `voice_muted`, `telegram_after_run`, а также `record_runs` (если работа по записи видео влита; иначе не добавлять).
  Ключи должны быть в `settings.DEFAULTS` GitS (для `record_runs` — добавить там, поле в дашборде — на вкладке модуля
  через `settings_schema` не годится, это общая настройка: добавить в форму настроек GitS рядом с
  `telegram_after_run`).
- **RW-8 (ОБЯЗАТЕЛЬНО).** `state` модуля раз в 5 с и при изменении: `summary` — «игра: <нет | меню | колония
  «<имя>»>, режим агента: <режим>, цель: <главная цель>»; `data` — то, что сейчас отдаёт `GET /api/game`, плюс
  `"game_running": bool` (порт мода открыт и колония загружена). Ядро берёт отсюда данные для дашборда вместо
  `game_bridge.game_state()` (правка `dashboard.py` в GitS) и условие профиля (`module_state: {rimworld:
  {game_running: true}}`).

## 6. События

- **RW-9 (ОБЯЗАТЕЛЬНО).** Нынешние вызовы `link.event(...)` и игровые события, которые сейчас пишутся в
  `events_log` с видом `game`, становятся событиями модуля. Объявить в `emits`:

| kind | Источник сейчас | default_urgency | Примечание |
|---|---|---|---|
| `rimworld.colonists` | `link.event("colonists", ...)` | 0 | `data` как сейчас; ядро обновляет отношения с пешками (перенести нынешнюю обработку `colonists` из `core_service` GitS в обработчик события) |
| `rimworld.run_ended` | `link.event("run_ended", ...)` | 3 | `text` — факт об итоге; пост-итог в Telegram см. RW-12 |
| `rimworld.raid` | события игры «набег», «осада», «атака» | 5 | `addressed_to_me: false` |
| `rimworld.death` | гибель колониста | 4 | |
| `rimworld.fire` | пожар | 4 | |
| `rimworld.letter` | игровые письма | 2 | |
| `rimworld.goal` | цель достигнута или провалена | 3 | |
| `rimworld.run_video` | видео забега готово (если запись влита) | 2 | `data`: пути к записи, `scenario.json` |

  Точное сопоставление нынешних событий игры с этими видами — по `statediff.py` и `controller.py`; если вид события
  не подходит ни под один — `rimworld.letter` с `urgency_hint` по нынешней важности. Комментатор продолжает говорить
  сам через `say` (verbatim): события — для ядра, чтобы оно знало и могло упомянуть вне игры.

## 7. Инструменты

- **RW-10 (ОБЯЗАТЕЛЬНО).** Манифест строится автоматически из MCP-сервера `rimworld_mcp.py` (66 инструментов на
  2026-10-09) в процессе модуля: импортировать объект FastMCP, получить список инструментов и их `inputSchema`
  (публичным API FastMCP: `await mcp.list_tools()`), перевести схему в упрощённые `params` (та же логика, что
  `environments/sources._schema_to_params` в GitS — скопировать), исключить `game_*` и `get_pending_events` (как в
  нынешнем GitS-манифесте), `risk: auto`, `availability: profile`, `timeout_s: 60`, `keywords` — как в
  `rimworld.json`. Обработчик — вызов функции инструмента через `asyncio.to_thread` (функции синхронные). Описание —
  первая строка docstring, до 300 символов.
- **RW-11 (ОБЯЗАТЕЛЬНО).** Инструмент `order`: params `text` (string), `risk: auto`, `availability: profile`,
  `timeout_s: 15`; обработчик — `controller.handle_order(text)`, возвращает `{"accepted": bool}`. В ядре GitS
  `companion.game_order` (сейчас `game_bridge.order`) заменить на: для каждого модуля из `focus_modules` активного
  профиля, у которого есть `capabilities: ["game"]` и инструмент `order`, вызвать его (тайм-аут 15 с); `accepted:
  true` → обращение обработано игрой, как сейчас. Порядок и поведение `companion.on_user_message` не менять.
- **RW-12 (ОБЯЗАТЕЛЬНО).** Пост-итог после забега: при конце забега, если `telegram_after_run`, модуль сам вызывает
  `client.request("core.tool.call", {"tool": "telegram.post", "args": {"topic": <prompt как сейчас>}})`. Обработку
  `run_ended` → Telegram в ядре GitS удалить (ТЗ 10, TG-15).
- **RW-13 (ОБЯЗАТЕЛЬНО).** MCP-сервер `rimworld_mcp.py` и `run_mcp.py` остаются: их используют контуры игры на Claude
  внутри модуля. GitS-манифест `environments/manifests/rimworld.json` удалить.
- **RW-14 (ОБЯЗАТЕЛЬНО).** Подграф: `graph: true`, обработчик `graph.get` возвращает структуру подграфа (сейчас в GitS
  `_DEFAULT_SUBGRAPHS["rimworld"]`; живой граф режимов игры, если он есть в коде модуля — его). `_DEFAULT_SUBGRAPHS`
  в ядре остаётся запасным.

## 8. Расход Claude и запросы к ядру

- **RW-14a (ОБЯЗАТЕЛЬНО).** Живой подграф уже есть: `game_api.handle_api_graph` (`/api/graph`, режимы игры из
  `game_modes.py`). Обработчик `graph.get` вызывает ту же функцию. `/api/graph` на :8767 остаётся для оверлея.
- **RW-14b (ОБЯЗАТЕЛЬНО).** Отметки оценок событий: дашборд GitS пересылает `POST /api/appraisal_label` на
  `:8767/api/appraisal_label` (`RIMWORLD_AGENT_URL`). Оставить пересылку по HTTP (это пульт), переменную
  переименовать в `RIMWORLD_MODULE_URL` (старая — запасная, как в RW-2).
- **RW-14c (ОБЯЗАТЕЛЬНО).** Классификатор: `main._start_decider` (запуск `decider_server.py` из `agent/.venv-decider`)
  удалить — сервис запускает супервизор ядра (ТЗ 03 ATT-1/ATT-2). Клиент `decider.py` и `appraisal.py` остаются,
  адрес прежний (`:8768`). `decider_server.py` и `eval_decider.py` удалить из модуля (они в `GitS/services/decider`).
- **RW-15 (ОБЯЗАТЕЛЬНО).** После каждого запуска Claude в контурах игры (`glados_agent.py`, `executor_agent.py`, где
  есть `ResultMessage`) — `client.request("core.claude_usage", {"tokens": claude_battery.tokens_of(usage), "source":
  "rimworld.<контур>"})` без ожидания результата в критическом пути (отдельной задачей). Сейчас это делает `usage_report.push(tokens, source,
  limit_text)` (HTTP `POST /api/claude_usage`): заменить реализацию `usage_report` на `core.claude_usage` с теми же
  полями (`limit_text` — текст исчерпанного лимита; ядро вызывает `claude_quota.note`, как нынешний
  `/api/claude_usage`), вызывающий код не менять.
- **RW-16 (ЖЕЛАТЕЛЬНО).** Запасной уровень управления игрой через экран и поиск в интернете из контуров игры —
  `core.tool.call("desktop.*")` и `core.web_search`, если контуры игры это где-то используют. Если не используют —
  ничего не делать.

## 9. Запись видео забегов (только если работа из раздела 2 влита)

- **RW-17 (ОБЯЗАТЕЛЬНО, условно).** Запись (`run_recorder.capture`), режиссёр камеры, наблюдатель за игрой, звук и
  сценарий (`scenario.py`) остаются в модуле. Монтаж по сценарию (`montage_args`, `clip_args`, `process`) остаётся
  как запасной до появления `montage-module`. Новая настройка модуля `settings_schema.auto_montage` (boolean, по
  умолчанию `false`): если `true` и модуль `montage` подключён, после конца записи модуль вызывает
  `core.tool.call("montage.start", {...})` (параметры — ТЗ 20, источник `rimworld_run`) вместо своего монтажа.
  Сама эта ветка кода пишется в ТЗ 20 (здесь — только настройка и место вызова с `TODO(ТЗ 20)`).

## 10. Удаление старого в GitS

- **RW-18 (ОБЯЗАТЕЛЬНО).** Удалить: `game_bridge.py`; эндпоинты `/api/say`, `/api/speech_status`, `/api/emotion`,
  `/api/event`, `/api/tool`, `/api/web_search`, `/api/claude_usage` и связанные `_SPEECH` в `dashboard.py`;
  `environments/manifests/rimworld.json`; пункт трея «агент игры» (`start_game_agent`, `restart_game_agent`,
  `GAME_AGENT_DIR`); `_LINKED_SETTINGS`. Тесты, которые их проверяли, удалить или переписать на модуль.
- **RW-19 (ОБЯЗАТЕЛЬНО).** Подграф RimWorld в `/api/modes` теперь через `graph.get` (ТЗ 02, CORE-22).

## 10a. Медленные тесты

- **RW-19a (ЖЕЛАТЕЛЬНО).** Два теста в `test_run_recorder.py` запускают настоящий ffmpeg (~55 с каждый). Пометить
  `@pytest.mark.slow`, в `check.ps1` по умолчанию `-m "not slow"`, полный прогон — `.\check.ps1 -All`.

## 11. Тесты

- **RW-20 (ОБЯЗАТЕЛЬНО).** `rimworld-module/agent/tests/` против `FakeCore`:
  - манифест строится из `rimworld_mcp` и валиден; исключённые инструменты отсутствуют; у каждого инструмента есть
    обработчик;
  - `tool.call` инструмента вызывает функцию MCP (функция подменена) и возвращает её строку;
  - `order` → `controller.handle_order` (подмена) → `{"accepted": ...}`;
  - `CoreLink`: `say`, `emotion`, `is_speaking`/`last_user_ts` из темы `presence`, общие настройки из темы `settings`
    попадают в `settings`;
  - `state` содержит `game_running`;
  - `run_ended` при `telegram_after_run` → `core.tool.call` с `telegram.post`.
  Прежние тесты модуля проходят.
- **RW-21 (ОБЯЗАТЕЛЬНО).** GitS: тест вызова `order` у модуля фокуса с `capabilities: ["game"]` в
  `companion.on_user_message` (эхо-модуль с `capabilities: ["game"]` и `order` в тестовом манифесте).

## 12. Приёмка

1. Папка `C:\MyProjects\rimworld-module`, `origin` указывает на новое имя (если GitHub переименован), `git worktree
   list` без ошибок.
2. `.\check.ps1` зелёный в обоих репозиториях; `rg "rimworld-agent"` находит только историю (`CHANGELOG`, `docs/tz`).
3. Ручная проверка: ядро запускает модуль; при запуске игры включается профиль RimWorld; агент играет, комментирует
   голосом через ядро; «пусть колонисты построят стену» доходит до игры как приказ; «что в колонии?» отвечает
   инструментом модуля; набег — она реагирует (решение внимания D4 видно в журнале); подграф в дашборде живой;
   оверлей OBS на :8767 работает; конец забега при `telegram_after_run` публикует пост.
4. Выключение модуля в дашборде останавливает агента игры, ядро работает.
