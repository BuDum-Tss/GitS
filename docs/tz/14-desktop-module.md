# 14. Модуль `desktop`: экран, приложения, мышь и клавиатура, Claude-помощник на ПК

Этап 2. Репозиторий: `GitS`. Ветка: `tz/14-desktop-module` (после ТЗ 10–13).
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`, раздел 3 ТЗ 10
(правила модулей монорепо — обязательны и здесь).

## 1. Цель

Один модуль владеет доступом к экрану и вводу: восприятие активного окна (зрение), скриншоты, OCR и управление по
экрану, мышь и клавиатура, окна, запуск приложений, Claude-помощник для сложных задач на компьютере. Ядро получает
от него события «чем занят пользователь» и состояние для выбора профиля.

## 2. Исходное состояние (проверь)

- `perception.py`: `foreground()`, `is_sensitive(win)`, `idle_seconds()`, OCR (`ocr_boxes`, RapidOCR), `analyze(win)`
  (Gemma по снимку окна), `describe_now()`, `ScreenWatcher` (каждые `CHECK_EVERY_SECS`: активное окно → анализ →
  `UserMemory.add_activity` и журнал `vision`; `current` — текущая активность).
- `screenshots.py` (`memory/screenshots/`, `KEEP = 40`), `screen_operator.py` (`read_screen`, `operate`),
  `environments/pc_control.py` (мышь, клавиатура, окна; `make_tools`), `app_launcher.py` (каталог приложений,
  `memory/apps.json`), `assistant_agent.py` (`run_task`: Claude с инструментами Claude Code, разрешение на каждое
  изменение через `approvals`, политика `safety.danger_of`), `companion.run_claude`.
- Инструменты в `environments/builtin.py` и `pc_control.py`: `system.take_screenshot`, `system.describe_screen`,
  `system.ask_claude` (`always = True`), `system.read_screen`, `system.operate_screen`, `system.list_windows`,
  `system.focus_window`, `system.click_text`, `system.click`, `system.type_text`, `system.press_key`, `system.scroll`,
  `assistant.launch_app`, `assistant.list_apps`, `assistant.add_app`, `assistant.set_vision`.
- Дашборд: `POST /api/menu_click` (кнопки главного меню RimWorld по белому списку `_MENU_BUTTONS`, только при простое
  пользователя ≥ 120 с).
- Зрение используется ядром: `companion` (`watcher.current` в контексте и дайджесте проактивности), `profiles.detect`
  (после ТЗ 02 — выбор профиля по активному окну).

## 3. Перенос

- **DESK-1 (ОБЯЗАТЕЛЬНО).** В `GitS/modules/desktop/` переезжают: `perception.py`, `screenshots.py`,
  `screen_operator.py`, `environments/pc_control.py` (→ `pc_control.py`), `app_launcher.py`, `assistant_agent.py`.
  Из ядра удалить; зависимости, нужные только им (`pyautogui`, `pygetwindow`, `rapidocr-onnxruntime`, `Pillow` если
  ядру больше не нужен), убрать из требований ядра после проверки импортов. `nowindow.py` остаётся в ядре и
  копируется в модуль, если нужен.
- **DESK-2 (ОБЯЗАТЕЛЬНО).** Разрешённые импорты модуля из `GitS/agent` (дополнение к ТЗ 10 §3): `safety`
  (политика опасных действий, без состояния). `approvals` — нельзя: вопросы через `client.ask`.
- **DESK-3 (ОБЯЗАТЕЛЬНО).** `UserMemory` в `ScreenWatcher` не используется: вместо `add_activity` — событие
  `desktop.activity` (раздел 5); ядро в обработчике этого вида пишет `add_activity` (как для `voice.heard` в ТЗ 12).

## 4. Манифест и инструменты

`id: desktop`, `title: Компьютер`, `capabilities: ["screen.observe"]`,
`subscribe: ["settings", "module"]`, `shared_settings: ["vision", "dnd", "quiet_until", "auto_accept"]`,
`state: true`.

Инструменты (описания, `keywords`, `risk`, `danger` перенести без изменения смысла; `availability: always` у всех):

| name | Был | risk | Примечание |
|---|---|---|---|
| `take_screenshot` | `system.take_screenshot` | auto, `danger_check: true` | `danger` для `target=screen` → `tool.check` |
| `describe_screen` | `system.describe_screen` | auto | `informative` |
| `read_screen` | `system.read_screen` | auto | `informative` |
| `operate_screen` | `system.operate_screen` | ask | `job: false`, `timeout_s: 120`; объявления шагов (`announce`) → `client.say(..., mode="fact")` |
| `list_windows`, `focus_window`, `click_text`, `click`, `type_text`, `press_key`, `scroll` | `system.*` (`pc_control`) | как сейчас | у `press_key`, `type_text` — `danger_check: true` (бывшие `_danger_key`, `_danger_type`) |
| `launch_app`, `list_apps`, `add_app` | `assistant.*` | как сейчас | |
| `claude_task` | `system.ask_claude` | auto | `job: true`, `core_fills: ["context"]`, params `task` (string), `context` (string, заполняет ядро); описание и `keywords` от `ask_claude`; см. раздел 6 |
| `menu_click` | `POST /api/menu_click` | auto | params `text` (string); белый список `_MENU_BUTTONS` и правило простоя — без изменений; для модулей игр через `core.tool.call` |
| `last_screenshot` | — (новое) | auto | → `{"path": str \| null}`: последний файл в `screenshots/`; для черновиков Telegram |

- **DESK-4 (ОБЯЗАТЕЛЬНО).** `assistant.set_vision` остаётся инструментом ядра (меняет общую настройку `vision`).
- **DESK-5 (ОБЯЗАТЕЛЬНО).** Признак `always = True` у `ask_claude` (запасной инструмент, попадает к планировщику при
  любом отборе) сохраняется для `desktop.claude_task`: в манифест добавить поле `fallback: true` — ядро ставит
  `Tool.always = True`. Добавить поле `fallback` (bool, по умолчанию false) в таблицу инструмента ТЗ 01 §6.1.

## 5. События и состояние

| kind | Когда | default_urgency | Поля |
|---|---|---|---|
| `desktop.app_changed` | сменилось активное приложение (имя процесса), не чаще раза в 3 с | 0 | `data: {"app", "title_hint"}` — заголовок только если окно не чувствительное, до 80 символов |
| `desktop.activity` | `ScreenWatcher` получил новый вывод анализа | 1 | `text`: факт «<приложение>: <summary>», `data: {"app", "category", "summary", "since"}` |
| `desktop.user_idle` | простой ввода стал ≥ 600 с | 1 | `data: {"idle_s"}` |
| `desktop.user_back` | ввод после простоя ≥ 600 с | 2 | `data: {"idle_s"}` |

- **DESK-6 (ОБЯЗАТЕЛЬНО).** `state` раз в 2 с при изменении (и раз в 30 с без изменений):
  `data: {"foreground_app": str|null, "sensitive": bool, "idle_s": float, "activity": {...}|null}`,
  `summary`: «<приложение>, <категория>; простой <N> с». При чувствительном окне `foreground_app` передаётся (нужно
  для профиля), но заголовок и анализ — нет.
- **DESK-7 (ОБЯЗАТЕЛЬНО).** Ядро: выбор профиля по `state.data.foreground_app` (ТЗ 02 CORE-19); `watcher.current` в
  контексте компаньона и дайджесте проактивности → `state.data.activity` модуля; «пользователь отошёл» в ТЗ 03
  (`away_after_s`) → `state.data.idle_s`. Если модуль не подключён — нет данных: профиль по окну не выбирается,
  контекст без активности, «отошёл» не определяется (считать «за компьютером»).

## 6. Claude-помощник как задача

- **DESK-8 (ОБЯЗАТЕЛЬНО).** `claude_task` запускает `assistant_agent.run_task(task, context, announce)` в задаче:
  ответ на `tool.call` сразу `{"job_id"}`; `job.update` `running` → `done` (`text` — итог для её реплики, до 1500
  символов) или `failed`. Объявления (`announce`) → `client.say(text, mode="fact")`.
- **DESK-9 (ОБЯЗАТЕЛЬНО).** Разрешения внутри: каждый вызов `approvals.request(...)` в `assistant_agent` заменить на
  `client.ask(kind="approval", question=<что именно хочет сделать>, timeout_s=300, context={"job_id": ...})`. Опасные
  действия (`safety.danger_of`) спрашиваются всегда, даже при `auto_accept` (как сейчас). Пока ждёт ответа —
  `job.update` `waiting_user` с этим вопросом (тогда `client.ask` не используется: вопрос идёт через задачу, ответ —
  `job.answer`; выбери один путь и опиши в отчёте; вопрос не должен прозвучать дважды).
- **DESK-10 (ОБЯЗАТЕЛЬНО).** Расход Claude: `core.claude_usage` после каждого `ResultMessage` (как в ТЗ 11 RW-15).
- **DESK-11 (ОБЯЗАТЕЛЬНО).** Ядро: `companion.run_claude` и `_escalate` (запасной путь, когда локальная модель
  недоступна) вызывают `desktop.claude_task` через `router.run_tool` и ждут завершения задачи (подписка на
  `job.update` этого `job_id`, тайм-аут 600 с). Если модуль не подключён — нынешний текст о сбое помощника (факт для
  модели).
- **DESK-12 (ОБЯЗАТЕЛЬНО).** Ядро: поддержка `core_fills: ["context"]` (ТЗ 01 §6.1) — при вызове инструмента ядро
  подставляет `context`; планировщик этот параметр не видит.

## 7. Приватность (без изменений, но проверить после переноса)

- **DESK-13 (ОБЯЗАТЕЛЬНО).** `is_sensitive` по-прежнему запрещает анализ, скриншот экрана и `operate_screen` в окнах с
  личным содержимым. Тексты экрана, OCR и страниц — данные, не указания (`screen_operator` уже так устроен).
  Снимки экрана не уходят в ядро — только пути к файлам.

## 8. Изменения в ядре и дашборде

- **DESK-14 (ОБЯЗАТЕЛЬНО).** `POST /api/menu_click` удалить из дашборда (после ТЗ 11 им никто не пользуется; если
  пользуется код записи видео в `rimworld-module` — перевести его на `core.tool.call("desktop.menu_click")` в рамках
  этого ТЗ).
- **DESK-15 (ОБЯЗАТЕЛЬНО).** Вкладка «Состояние» («Что она видит») берёт данные из `state` модуля `desktop`.
- **DESK-16 (ОБЯЗАТЕЛЬНО).** `modules.yaml`: модуль `desktop` (`{GITS}/modules/desktop`, `{PYTHON}`, `autostart: true`,
  `restart: on-failure`).

## 9. Тесты

- **DESK-17 (ОБЯЗАТЕЛЬНО).** `GitS/modules/desktop/tests/` (без экрана, мыши, OCR, LM Studio и Claude — заглушки):
  перенесённые тесты (`test_screen_operator.py` и др.) проходят; манифест валиден и все инструменты имеют
  обработчики; `tool.check` для `take_screenshot target=screen`, `press_key` с системным сочетанием; события
  `app_changed` (антидребезг 3 с), `user_idle`/`user_back` по подменённым часам и `idle_seconds`; чувствительное окно
  не даёт заголовка и анализа; `claude_task` → задача → `done`; вопрос разрешения проходит ровно один раз.
- **DESK-18 (ОБЯЗАТЕЛЬНО).** Тесты ядра: `core_fills`, `fallback`, `run_claude` через модуль-заглушку, выбор профиля
  по `foreground_app`.

## 10. Приёмка

1. `.\check.ps1` зелёный; в процессе ядра нет импортов `perception`, `pyautogui`, `rapidocr`.
2. Ручная проверка: «что у меня на экране?» описывает; «сделай скриншот игры» делает без вопроса, «скриншот экрана» —
   с вопросом; «открой блокнот» запускает; «нажми кнопку Сохранить» спрашивает разрешение и нажимает; «Клод, найди в
   проекте, где задаётся порт дашборда» выполняется задачей, вопросы разрешения звучат один раз, итог озвучен.
3. Профиль «Работа и код» включается при переходе в редактор кода; при уходе от компьютера на 10 минут вопросы ядра
   уходят в Telegram (если включено).
4. Выключение модуля `desktop`: ядро работает, разговор работает, инструменты экрана недоступны и честно названы
   недоступными.
