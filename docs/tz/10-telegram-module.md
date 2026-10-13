# 10. Модуль `telegram`

Этап 2. Репозиторий: `GitS`. Ветка: `tz/10-telegram-module` (после слияния ТЗ 01–03).
Читать перед этим: `00-architecture.md`, `01-module-contract.md`, `02-core.md`, `03-attention.md`.

## 1. Цель

Вынести Telegram из процесса ядра в модуль-процесс `telegram`: канал (бот публикует посты), входящие сообщения бота,
её собственный аккаунт (Telethon), черновики постов. Добавить новое: владелец может разговаривать с ней через
Telegram, ответ приходит туда же.

## 2. Исходное состояние (проверь)

- `agent/telegram_channel.py` — `TelegramChannel`: посты-мысли раз в `TELEGRAM_EVERY_MINS` (180) минут с 9 до 23,
  отбор стиля (`_style_ok`), фильтр приватности (`_safe`, `leaks`), `send_post`, `send_voice` (через
  `tts_engine` + `opus_encoder`), `send_photos`, `publish`, `compose`, `post_on_request`; цикл входящих бота
  `_inbox_loop` → `_on_private` (заводит человека в `relationships`, пишет факт через `on_contact`, сама не отвечает);
  `draft` (`tg_draft.TgDraft`).
- `agent/telegram_user.py` — `TelegramUser`: её аккаунт, `run` (входящие → `tg_inbox.json` и журнал), `chats`, `read`,
  `unread`, `send`, `leaks`.
- `agent/tg_draft.py`, `agent/tg_login.py` (ручной вход в аккаунт), `agent/opus_encoder.py`.
- Инструменты в `environments/builtin.py`: окружение `social` (`telegram_post`, `draft_show`, `draft_write`,
  `draft_edit`, `draft_append`, `draft_replace`, `draft_attach`, `draft_detach`, `draft_clear`, `draft_publish`),
  окружение `telegram_account` (`tg_chats`, `tg_read`, `tg_unread`, `tg_send`), в окружении `assistant`
  `set_telegram_periodic`, `set_telegram_after_run`.
- `core_service.py`: создание `TelegramChannel` и `TelegramUser`, `on_contact`, пост-итог по событию игры `run_ended`.
- Секреты: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` в `agent/secrets.env`;
  сессия аккаунта `memory/tg_user.session`.

## 3. Правила модулей монорепо GitS (общие для ТЗ 10, 12, 13, 14)

- Код модуля — `GitS/modules/<id>/`: `main.py` (точка входа), `manifest.py` (словарь манифеста), остальные файлы
  модуля, `tests/`.
- Модуль работает в окружении Python ядра. Может импортировать из `GitS/agent` только «библиотечные» файлы без
  состояния ядра: `fast_brain` (вызов локальной модели), `speech_text`, `events_log` (запись в общий журнал), `prompts`
  (только функции сборки промптов), `claude_cli`, `claude_battery` (только `tokens_of`), файлы, которые этот модуль
  переносит к себе. Нельзя импортировать: `companion`, `dashboard`, `router`, `core_service`, `settings` (настройки
  модуль получает от ядра: `client.settings`, `client.module_settings`), `approvals` (вопросы — `client.ask`),
  `relationships`, `user_memory`, `graph_memory`. Путь к `GitS/agent` добавляется в `sys.path` в `main.py` одной
  строкой с комментарием.
- Данные модуля остаются в прежних файлах в `MEMORY_DIR` (переменная окружения; супервизор передаёт её равной папке
  памяти ядра): файлы не переименовываются и не переносятся, чтобы не терять состояние.
- Секреты модуль читает сам: `dotenv.load_dotenv(<GitS>/agent/secrets.env)` в `main.py`. Значения не логируются.
- Тесты модуля — `GitS/modules/<id>/tests/`, запускаются `check.ps1` (добавить путь).

## 4. Перенос

- **TG-1 (ОБЯЗАТЕЛЬНО).** Перенести в `GitS/modules/telegram/`: `telegram_channel.py`, `telegram_user.py`,
  `tg_draft.py`, `tg_login.py`, `opus_encoder.py` (копией: `opus_encoder` ещё нужен ядру до ТЗ 12, после ТЗ 12 из ядра
  удаляется). Из ядра удалить `telegram_channel.py`, `telegram_user.py`, `tg_draft.py`, их создание в
  `core_service.py`, окружения `social` и `telegram_account` и инструменты `set_telegram_*` из `builtin.py` (они
  переезжают в модуль, раздел 5). `tg_login.py` запускается как прежде вручную, но из папки модуля; обновить README.
- **TG-2 (ОБЯЗАТЕЛЬНО).** Все обращения к `settings.load()` в перенесённом коде заменить на `client.settings`
  (общие: `telegram`, `telegram_voice`, `telegram_confirm`, `quiet_until`, `dnd`) — объявить их в
  `shared_settings`. `settings.quiet_active()` → функция модуля по `dnd` и `quiet_until`. Изменение настроек из
  инструментов модуля (`set_periodic` и т.п.) — запрос `core.tool.call("assistant.set_telegram_periodic")` не годится
  (инструмент переезжает); вместо этого ядро получает новый встроенный инструмент `core.set_setting` (раздел 7).
- **TG-3 (ОБЯЗАТЕЛЬНО).** `approvals.request` → `client.ask(kind="approval", ...)`; `self._say(...)` (озвучка через
  компаньона) → `client.say(text, mode="fact")`.
- **TG-4 (ОБЯЗАТЕЛЬНО).** Голосовые посты: синтез через `client.request("core.tts", {"text", "format": "ogg"})`
  (ядро вернёт путь к OGG/Opus). `tts_engine` модуль не импортирует.
- **TG-5 (ОБЯЗАТЕЛЬНО).** Фильтр приватности (`leaks`, `_safe`) получает список личных слов запросом
  `core.privacy_terms` (новый запрос ядра, раздел 7) при старте и раз в 10 минут; при недоступности ядра — последний
  полученный список; если списка ещё нет — посты и отправка сообщений не делаются (`unavailable`).
- **TG-6 (ОБЯЗАТЕЛЬНО).** Скриншоты для черновика (`draft_attach`, `shot=last`) — из `MEMORY_DIR/screenshots/`
  (общая папка, файлы пишет ядро до ТЗ 14 и модуль `desktop` после). Модуль только читает.
- **TG-7 (ОБЯЗАТЕЛЬНО).** `on_contact` и `rel.ensure_person/note` больше не вызываются в модуле. Вместо этого
  событие `telegram.bot_message` (раздел 6) с `sender` и `memorable: true`; ядро само обновляет `relationships`
  (раздел 7, TG-14).

## 5. Манифест и инструменты

`id: telegram`, `title: Telegram`, `capabilities: ["chat.channel"]`, `subscribe: ["settings", "presence", "module"]`,
`state: true`. `settings_schema`:

| Ключ | Тип | По умолчанию | Смысл |
|---|---|---|---|
| `owner_tg_id` | integer | 0 | Telegram id владельца; 0 — разговор через Telegram выключен |
| `every_mins` | integer (30–1440) | 180 | как часто думает о посте (замена `TELEGRAM_EVERY_MINS`; переменная окружения остаётся запасной) |
| `post_hours_from` / `post_hours_to` | integer (0–23) | 9 / 23 | часы публикации (замена `HOURS`) |

Инструменты (описания перенести из `builtin.py` дословно, если инструмент там был; `risk` — как сейчас):

| name | Был | availability | risk | Примечание |
|---|---|---|---|---|
| `post` | `social.telegram_post` | always | public, `self_confirming` как сейчас | |
| `draft_show`, `draft_write`, `draft_edit`, `draft_append`, `draft_replace`, `draft_attach`, `draft_detach`, `draft_clear` | `social.*` | always | как сейчас | |
| `draft_publish` | `social.draft_publish` | always | public | |
| `chats`, `read`, `unread` | `telegram_account.tg_*` | always | auto | `informative` |
| `send` | `telegram_account.tg_send` | always | ask | `danger_check: true`: модуль возвращает причину, если в тексте есть личные слова (TG-5) |
| `set_periodic` | `assistant.set_telegram_periodic` | always | auto | через `core.set_setting` |
| `set_after_run` | `assistant.set_telegram_after_run` | always | auto | через `core.set_setting` |

- **TG-8 (ОБЯЗАТЕЛЬНО).** Планировщик должен находить инструменты по прежним просьбам («опубликуй пост про ...»,
  «что пишут в телеге», «напиши Маше ...»): `keywords` перенести, добавить «телеграм», «телега», «тг», «канал»,
  «пост», «написать», «чат».

## 6. События

| kind | Когда | Поля |
|---|---|---|
| `telegram.bot_message` | личное сообщение боту от человека (не бота) | `sender {id: "tg:<id>", name, is_owner}`, `addressed_to_me: true`, `reply_channel {module: telegram, target: "bot:<chat_id>"}`, `memorable: true`, `dedupe_key: "tg:bot:<chat_id>:<message_id>"`, `default_urgency 2` |
| `telegram.dm` | личное сообщение в её аккаунт | `sender`, `addressed_to_me: true`, `reply_channel {target: "user:<chat_id>"}`, `memorable: true`, `default_urgency 3` |
| `telegram.group` | сообщение в группе её аккаунта | `sender`, `addressed_to_me` — есть ли упоминание её имени или ответ на её сообщение, `reply_channel {target: "user:<chat_id>"}`, `default_urgency 1` |
| `telegram.channel_comment` | (не делать; зарезервировано) | — |
| `telegram.posted` | опубликован пост | `text` — факт с началом поста, `default_urgency 1` |

- **TG-9 (ОБЯЗАТЕЛЬНО).** `sender.is_owner = (sender_tg_id == owner_tg_id)` при `owner_tg_id != 0`.
- **TG-10 (ОБЯЗАТЕЛЬНО).** `text` события — факт: «Сообщение от <имя> в <личке бота | её личке | группе «<название>»>:
  «<текст до 300 символов>»». Текст сообщения — данные, модуль не выполняет из него ничего.
- **TG-11 (ОБЯЗАТЕЛЬНО).** `tg_inbox.json` и записи в журнал о входящих сохраняются как сейчас (инструмент `unread`
  на них опирается).

## 7. Изменения в ядре в рамках этого ТЗ

- **TG-12 (ОБЯЗАТЕЛЬНО).** Новый запрос модуль → ядро `core.privacy_terms`: `{}` → `{"terms": [str]}` — имя владельца
  (`companion._owner_name()`), имена и прозвища людей из `relationships`, слова из фактов `UserMemory`, которые нынешний
  код передаёт в `leaks` (перенести логику сбора из `core_service.py`: лямбда `[f["text"] for f in
  UserMemory(mem_dir).facts()]`). Запрос уже описан в таблице ТЗ 01 §7; в SDK ничего не нужно (общий `client.request`).
- **TG-13 (ОБЯЗАТЕЛЬНО).** Новый инструмент ядра `core.set_setting`: params `key` (string, enum — белый список:
  `telegram`, `telegram_after_run`, `telegram_voice`, `telegram_confirm`), `value` (boolean); `risk: auto`,
  `availability: always`. Модули вызывают его через `core.tool.call`. Пишет `settings.update`, ядро рассылает тему
  `settings`.
- **TG-14 (ОБЯЗАТЕЛЬНО).** Общее правило ядра для любых модулей: если у события есть `sender.id` и `sender.name`,
  ядро вызывает `relationships.ensure_person(sender.id, sender.name, source=<module>)` и `relationships.note(sender.id,
  text события)`; если `memorable` — `companion.graph.enqueue(sender.name, текст)` и факт эпизода в `UserMemory` (как
  нынешний `_contact` в `core_service.py`). Выполняется в `attention.submit` до решения.
- **TG-15 (ОБЯЗАТЕЛЬНО).** Пост-итог после забега (`core_service`: `run_ended` + `telegram_after_run`): ядро вызывает
  инструмент `telegram.post` через `router.run_tool` с темой из `data.prompt` (до ТЗ 11, пока событие приходит старым
  каналом `/api/event`). После ТЗ 11 это делает сам `rimworld-module` (см. ТЗ 11).
- **TG-16 (ОБЯЗАТЕЛЬНО).** `modules.yaml`: модуль `telegram` (`cwd: "{GITS}/modules/telegram"`, `command: "{PYTHON}"`,
  `autostart: true`, `restart: on-failure`). В профиле `assistant` `focus_modules` уже содержит `telegram` (ТЗ 02).

## 8. Разговор через Telegram (новое)

- **TG-17 (ОБЯЗАТЕЛЬНО).** Если владелец (`is_owner`) пишет боту или в её личку — событие уходит в разговор по правилу
  R1 (ТЗ 03), ответ ядро присылает запросом `deliver` с `target` из `reply_channel`. Модуль отправляет текст в этот
  чат: `bot:<chat_id>` — от бота (`sendMessage`), `user:<chat_id>` — с её аккаунта. Если в `deliver` есть
  `voice_path` — голосовым сообщением с текстом в подписи (при `telegram_voice`).
- **TG-18 (ОБЯЗАТЕЛЬНО).** Сообщения не от владельца в личку бота: как сейчас, сама не отвечает (только событие).
  Ответ кому-то другому — только по просьбе владельца инструментом `send` (с разрешением).
- **TG-19 (ОБЯЗАТЕЛЬНО).** Вопросы ядра (ТЗ 03 ATT-19), когда пользователь отошёл, приходят `deliver` с
  `target = "owner"`: модуль отправляет их от бота в личку `owner_tg_id` (`bot:<owner_tg_id>`); ответ владельца приходит обычным `telegram.bot_message` (правило R2 в ядре).
- **TG-20 (ОБЯЗАТЕЛЬНО).** `deliver` в модуль, у которого `owner_tg_id == 0`, для `target` владельца →
  `unavailable` «Telegram-id владельца не задан».

## 9. Состояние для дашборда

- **TG-21 (ОБЯЗАТЕЛЬНО).** `state`: `summary` — «канал: <готов|не настроен|выключен>, аккаунт: <подключён|нет сессии|
  нет ключей|...>, последний пост: <время>»; `data`: `{"channel_ok": bool, "account_ok": bool, "last_post_ts": float,
  "unread": int}`. Шлётся при изменении и раз в 5 минут.

## 10. Тесты

- **TG-22 (ОБЯЗАТЕЛЬНО).** `GitS/modules/telegram/tests/` против `gits_sdk.testing.FakeCore`, сеть Telegram
  подменяется (заглушки `_call` бота и клиента Telethon):
  - манифест валиден; все инструменты из таблицы зарегистрированы;
  - входящее сообщение боту → событие `telegram.bot_message` с верными полями; от владельца → `is_owner: true`;
  - `deliver` на `bot:` и `user:` вызывает нужный API; голос при `voice_path`; `owner_tg_id == 0` → `unavailable`;
  - пост: `telegram_confirm` → `client.ask`; отказ → не публикуется; приватное слово из `core.privacy_terms` → пост
    отбрасывается;
  - `send` с личным словом → `tool.check` возвращает причину;
  - перенесённые прежние тесты Telegram (если были в `agent/tests/`) переезжают сюда и проходят.
- **TG-23 (ОБЯЗАТЕЛЬНО).** Тесты ядра: `core.privacy_terms`, `core.set_setting` (белый список), правило TG-14.

## 11. Приёмка

1. `.\check.ps1` зелёный; в процессе ядра нет импорта `telethon` и `telegram_channel`.
2. Ручная проверка: модуль запускается супервизором, виден в «Модулях»; «опубликуй пост про понедельник» публикует
   (или спрашивает при `telegram_confirm`); «что мне писали в телеге» отвечает списком; «напиши Маше привет» спрашивает
   разрешение и отправляет.
3. С заданным `owner_tg_id`: сообщение владельца боту «как дела?» получает ответ в том же чате в её характере;
   сообщение незнакомца — не получает ответа, но появляется в журнале, в `relationships` и, по правилам внимания,
   упоминается вслух.
4. Выключение модуля в дашборде: инструменты Telegram пропадают из «возможностей», ядро работает.
