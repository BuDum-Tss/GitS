# RimWorld в отдельном контейнере: дизайн

Статус: инфраструктура контейнера готова 18.11.2026 (`docker/rimworld/`, сервис `rimworld` в `docker-compose.yml`, `workspaces/rimworld/agent/lease_host.py`, запись в `agent/peers.container.yaml`); образ собирается, живая проверка (аренда ядром) и код `HostBridge`/помощника ещё впереди. Запуск: `docker/core/README.md`, раздел «RimWorld». Цель: код модуля RimWorld (`workspaces/rimworld/agent/`) работает в своём контейнере `rimworld` как общее
окружение с арендой (как монтаж), а то, что без хоста Windows не работает, живёт в маленьком хостовом помощнике.

## Что остаётся на хосте, что уезжает

| Часть | Где | Почему |
|---|---|---|
| Игра RimWorld и мод Agent Bridge (HTTP `localhost:8765`) | хост | сама игра |
| `game_launcher.py` (запуск exe, поиск процесса, psutil, subprocess) | хостовый помощник | управляет процессами Windows |
| `game_watchdog.py` (окно игры, ctypes, быстрый старт в меню) | хостовый помощник | окна Windows |
| `game_ui.py` (pyautogui, win32gui: мышь, клавиатура, скриншоты окна) | хостовый помощник | ввод и экран хоста |
| `audio_capture.py` (WASAPI loopback), `run_recorder.py` (ffmpeg gfxcapture окна) | хостовый помощник | звук и окно хоста |
| Контроллер, инструменты узлов, `rimworld_mcp.py`, `core_link.py`, события, память модуля, `workspace_module.py`, манифест, оверлей `game_api.py` (порт 8767) | контейнер `rimworld` | общаются с игрой по HTTP и с ядром по WebSocket |

Мод напрямую из контейнера НЕ достижим: он слушает только `[::1]:8765` (IPv6), а на `127.0.0.1:8765` этого ПК сидит чужой процесс (другой проект), поэтому `host.docker.internal:8765` отвечает не модом. Запросы к моду идут через прокси хостового помощника (`<GITS_RIMWORLD_HOST_URL>/game-api/...`, токен помощника); `RIMWORLD_API` у сервиса не задаётся. Хостовый помощник тоже слушает
`127.0.0.1` и достижим по `host.docker.internal`.

## Режимы работы кода модуля

Режим выбирается переменной `GITS_RIMWORLD_HOST_URL`:
- пусто: **локальный** режим (как сейчас, модуль на хосте, хостовые части вызываются прямо в процессе; поведение не меняется);
- задан (`http://host.docker.internal:8796`): **удалённый** режим (контейнер), хостовые части вызываются по HTTP хостового помощника.

Между кодом модуля и хостовыми частями вводится интерфейс `HostBridge` (`workspaces/rimworld/agent/host_bridge.py`) с двумя
реализациями: `LocalHostBridge` (оборачивает существующие `game_launcher`, `game_watchdog`, `game_ui`, `run_recorder`, `audio_capture`
без изменения их логики) и `RemoteHostBridge` (HTTP-клиент). Контроллер и `main.py` зовут только `HostBridge`. В контейнерном режиме модули с
Windows-зависимостями (`pyautogui`, `win32*`, `psutil`, `ctypes.windll`, `soundcard`) не импортируются.

**Адрес мода** (`gamenet.base_url()`): `RIMWORLD_API`, а если она пуста и задан `GITS_RIMWORLD_HOST_URL`, то `<GITS_RIMWORLD_HOST_URL>/game-api`
(прокси помощника). Не локальное имя в адресе включает режим прокси: ко всем запросам к моду добавляется заголовок
`Authorization: Bearer <GITS_RIMWORLD_HOST_TOKEN>` (`gamenet.auth_headers()`), а «игра запущена» (`gamenet.port_open`) значит «`GET /ping` через
прокси ответил 200». Без обеих переменных адрес прежний, `http://localhost:8765`. Мод читается только короткими запросами (`/ping`,
`/state`, `/events` опросом раз в 2 с, `POST /command`): потоков (SSE, WebSocket) нет, прокси их не поддерживает.
Оверлей (`game_api`, порт 8767): `GITS_DASHBOARD_HOST` (по умолчанию `127.0.0.1`, в контейнере `0.0.0.0`), проверка Origin пропускает
`http://localhost:` и `http://127.0.0.1:` (опубликованный порт виден браузеру на этих адресах) плюс префиксы из `GITS_DASHBOARD_ORIGINS` (через запятую).

Запись забега в контейнере (`remote_recorder.RemoteRunRecorder`): захват (ffmpeg, звук) и файлы остаются на хосте. Модуль хранит метки, исход,
цели и события забега, а при закрытии отдаёт их хосту (`POST /record/finish`); сценарий, `recording.json` и монтаж делаются на хосте рядом с записью,
пути в ответе хостовые. Для наблюдателя за игрой в модуле лежит зеркало состояния записи (`current` с заглушками `proc.poll()` и `audio.ok`).

## Хостовый помощник (`workspaces/rimworld/host/`)

Процесс на хосте: aiohttp, `127.0.0.1:8796` (порт настраивается), токен `GITS_RIMWORLD_HOST_TOKEN` (заголовок `Authorization: Bearer`,
сравнение `hmac.compare_digest`, без токена не стартует). Оборачивает `LocalHostBridge`. Состав (точный контракт исполнитель фиксирует
в этом документе, раздел «Контракт помощника»):
- команды (POST): запуск игры, быстрый старт в меню, действия UI (клик, клавиши, скриншот окна, движения), старт и стоп записи забега, и т.п.;
- состояние (GET): игра запущена, окно видно, на переднем плане, подключён ли мод, запись идёт;
- события помощника (то, что раньше шло через notifier/callback: объявления о запуске, итоги записи, сбои сторожа): `GET /events?after=N`
  с long-poll (до 25 с), монотонные номера, журнал событий в памяти ограничен; `RemoteHostBridge` опрашивает и вызывает те же обработчики,
  что и локальный режим;
- безопасность: помощник отвечает только на `127.0.0.1`, токен не в журналах, тела запросов ограничены, UI-действия только по окну RimWorld.

## Контейнер `rimworld`

- Общий сервис с арендой, как монтаж (`docker-compose.yml`, сервис `rimworld`, `container_name: rimworld`; ядро берёт его в аренду по
  `control_url` из `agent/peers.container.yaml`, запись `external: true`, `ownership: shared`). Точка входа использует `gits_sdk.control.ControlServer`
  (образец: `workspaces/montage/agent/lease_host.py`): при аренде подключается к ядру хозяина по WebSocket (адрес и токен из `acquire`).
- Переменные: `GITS_CONTROL_TOKEN`, `GITS_CONTROL_PORT`, `GITS_RIMWORLD_HOST_URL`,
  `GITS_RIMWORLD_HOST_TOKEN`, `RIMWORLD_MEMORY_DIR` (общий том), папка персонажа (ресурсы хозяина аренды).
- Порт оверлея для OBS (`8767`) опубликован на `127.0.0.1` хоста.
- Образ без Windows-зависимостей: `docker/rimworld/Dockerfile`; секреты `.env` в контейнер не передаются (только токены управления и помощника).

## Критерии готовности

1. Локальный режим (без `GITS_RIMWORLD_HOST_URL`) работает как раньше, все существующие тесты модуля зелёные.
2. Контейнерный режим: модуль импортируется без Windows-пакетов (тест с подставным `sys.modules`), все вызовы хостовых частей идут через `HostBridge`.
3. `RemoteHostBridge` против настоящего сервера помощника в процессе (с поддельными хостовыми частями): команды, состояние, события, ошибки, токен.
4. Образ собирается, контейнер поднимается, `GET /status` и `GET /info` отвечают, аренда ядром и подключение к его шине проходят (живая проверка).
5. Агент входит в среду `rimworld`, получает события игры, рабочий цикл `core.work_tick` и тревоги идут через внимание v2 (как при локальном модуле).

## Контракт помощника

Реализация: `workspaces/rimworld/host/server.py` (aiohttp), клиент `RemoteHostBridge` в `agent/host_bridge.py`. Версия контракта 1 (`GET /health` -> `api`).

**Общее.** Все пути, кроме `GET /health`, требуют заголовок `Authorization: Bearer <GITS_RIMWORLD_HOST_TOKEN>`; без него или с чужим токеном
ответ 401 `{"error", "code": "unauthorized"}`. Тела запросов и ответов JSON UTF-8 (кроме скриншота и прокси). Любая ошибка:
`{"error": "<текст>", "code": "<код>"}`. Коды: `unauthorized` 401, `bad_request` 400 (неверное поле, точка вне окна, запрещённая клавиша),
`too_large` 413, `unknown_record` 404, `window_not_found` 409 (окна RimWorld нет), `launch_failed` 409 (запуск не вышел или запрещён
`GITS_NO_LAUNCH=1`), `stop_failed` 409 и `stop_busy` 409 (закрытие игры, `/game/stop`), `no_recorder` 503, `internal` 500. Лимиты тела: 64 КБ (команды), 16 МБ (`/record/finish`), 1 МБ (прокси). Помощник слушает только
`127.0.0.1:<GITS_RIMWORLD_HOST_PORT>` (по умолчанию 8796). Клиент при недоступности помощника бросает `HostUnavailable` (код `unavailable`), цикл опроса
событий при этом живёт и повторяет попытки с ростом паузы до 30 с.

| Метод и путь | Запрос | Ответ |
|---|---|---|
| `GET /health` (без токена) | | `{"ok": true, "api": 1, "epoch": "<id запуска>", "events_last": N}` |
| `GET /game/status` | | `{"running", "mod_ready", "window": "missing\|minimized\|background\|front", "user_idle_s", "launch_forbidden", "recording": <статус записи>\|null}` |
| `POST /game/launch` | `{"reason": "<кто и зачем, обязателен>", "wait_seconds": 0..180 (0), "windowed": true}` | `{"result": "<описание итога>"}`; помощник сам объявляет запуск (событие `announce`); 409 `launch_failed`; ответ приходит после ожидания мода |
| `POST /game/stop` | `{"graceful_s": 0..30 (10)}` | `{"stopped": true, "how": "graceful\|killed\|not_running"}`: окну RimWorld шлётся WM_CLOSE и до `graceful_s` с ждётся выход, иначе kill процесса; ответ после исчезновения процесса и закрытия порта мода 8765 (поэтому ждёт до `graceful_s` + ~15 с); 409 `stop_failed` (процесс не исчез), 409 `stop_busy` (закрытие уже идёт). Нужен инструменту `restart_game`: сохранение игры идёт отдельно командой мода `save_game` через прокси `/game-api/command` |
| `POST /game/quick_start` | `{}` | `{"pressed": bool}` (кнопка «Быстрый запуск (Dev)» в главном меню, проверка цвета как в `press_quick_start`) |
| `POST /game/show_window` | `{}` | `{"ok": true}`; 409 `window_not_found` |
| `GET /ui/screenshot` | | PNG бинарно (`image/png`), только окно RimWorld; 409 `window_not_found` |
| `GET /ui/window` | | `{"width", "height"}` окна |
| `POST /ui/click` | `{"x", "y", "button": "left\|right\|middle", "clicks": 1..3}` | `{"ok": true}`; координаты от левого верхнего угла окна, вне окна 400 |
| `POST /ui/move` | `{"x", "y"}` | `{"ok": true}` |
| `POST /ui/drag` | `{"x1", "y1", "x2", "y2", "button"}` | `{"ok": true}` |
| `POST /ui/type` | `{"text": "<до 500 символов>"}` | `{"ok": true}` |
| `POST /ui/key` | `{"key": "enter"}` (буква, цифра или имя pyautogui из 2..16 латинских букв и цифр; `win*`, `apps`, `sleep`, `printscreen` запрещены) | `{"ok": true}` |
| `POST /ui/scroll` | `{"x", "y", "amount": -100..100}` | `{"ok": true}` |
| `POST /record/start` | `{"run_id", "colony", "why"?}` | `{"status": <статус записи>\|null}` (null: запись не началась, нет ffmpeg); незакрытая прежняя запись откладывается |
| `GET /record/status` | | `{"status": <статус записи>\|null}` |
| `POST /record/ensure_alive` | `{"max_hours"?: число}` | `{"closed_id": "<id закрытого куска>"\|null, "status": <статус новой текущей>\|null}` (ffmpeg умер или кусок длиннее `max_hours`: хост закрывает кусок и начинает следующий) |
| `POST /record/ensure_audio` | `{}` | `{"restarted": bool}` |
| `POST /record/finish` | `{"id": "<id записи>"\|null (текущая), "outcome", "mode": "stop\|handoff\|montage", "markers": [[сек, "подпись", важность]], "goal": {..}\|null, "events": [..]\|null}` | `{"result": ...}`: `stop` -> `{"stopped": true}`; `handoff` -> описание для модуля монтажа (как `RunRecorder.handoff`) или null (запись пустая или короткая); `montage` -> итог собственного монтажа или null; 404 `unknown_record` (помощник перезапускался) |
| `GET /events?after=N&wait=S` | `after` номер последнего полученного (0: всё, что есть), `wait` 0..25 с (по умолчанию 25) | `{"epoch", "last", "first", "events": [{"n", "ts", "type", "data"}]}`; пусто по истечении `wait`; журнал в памяти хранит 500 последних |
| `GET/POST /game-api/<путь мода>?<query>` | тело (POST) и `Content-Type` как есть | статус, тело и тип ответа мода как есть |

**Статус записи**: `{"id", "run_id", "colony", "raw": "<путь на хосте>", "started": unix, "video_alive", "audio_present", "audio_ok", "audio_stopping", "audio_error", "has_sound"}`.

**События** (`type`, `data`): `announce` `{"text"}` (факт для пользователя: запуск игры, начало записи; клиент отдаёт его уведомителю модуля,
`game_launcher.set_notifier`, то есть `core_link.announce`); `log` `{"kind", "text", "data"}` (запись в протокол событий на хосте, клиент пишет её в `events_log`
модуля: «запись звука оборвалась» и т.п.); `record` `{"op": "start\|part_closed\|audio_restarted\|finish", ...}` (обработчик `on_record_event`, по умолчанию не задан). Клиент при подключении начинает
с `events_last` из `/health`, так что события до подключения не повторяются; смена `epoch` (помощник перезапустился) сбрасывает номер на 0.

**Прокси к моду** (`/game-api/...`). Мод слушает только `[::1]:8765`, принимает в заголовке `Host` только `localhost`, поэтому помощник ходит к цели из своей
`RIMWORLD_API` (по умолчанию `http://localhost:8765`, при отказе в соединении пробует `http://[::1]:8765`) и для локальных адресов сам ставит `Host: localhost:<порт>`.
Пробрасываются методы GET и POST, путь и query, тело, `Content-Type` и `Accept`; токен помощника до мода не доходит. Путь пустой или с `..` отклоняется (400), другие методы 405.
Ошибки: 502 `mod_unreachable` (мод не отвечает, игра закрыта), 504 `mod_timeout` (30 с), 502 `mod_response_too_large` (ответ мода больше 8 МБ), 502 `mod_error`;
коды мода (200, 4xx, 5xx) возвращаются как есть. Потоков (SSE, long-poll, WebSocket) модуль от мода не читает, поэтому прокси их не поддерживает.

**Запуск помощника** (хост, из папки `workspaces/rimworld/host`): `GITS_RIMWORLD_HOST_TOKEN=<секрет> python main.py` (PowerShell: `$env:GITS_RIMWORLD_HOST_TOKEN="<секрет>"; python main.py`).
Переменные: `GITS_RIMWORLD_HOST_TOKEN` (обязательна), `GITS_RIMWORLD_HOST_PORT` (8796), `RIMWORLD_API` (адрес мода для прокси), `GITS_RIMWORLD_HOST_MEMORY` (данные: протокол событий, видео; по умолчанию `host/memory`), `GITS_VIDEO_DIR`, `GITS_FFMPEG`, `GITS_RIMWORLD_EXE`.
Тесты: `cd workspaces/rimworld && python -m pytest agent/tests host -q -p no:cacheprovider -m "not slow and not game"`.
