# Ядро GitS в контейнере

Один контейнер на персонажа. В контейнере только ядро и окружения без железа (`telegram`, `twitch`, `obs`, все со владением `owned`; файл `agent/peers.container.yaml`,
его выбирает `GITS_PEERS_FILE`, прежнее имя `GITS_MODULES_FILE` читается как алиас).
Голос, питомец и «Компьютер» остаются на хосте и подключаются к ядру по WebSocket на опубликованный порт.

## Запуск
Из корня репозитория (секреты и `GITS_TOKEN` берутся из `.env` хоста, параметры из `docker/core/glados.env`):

    docker compose -f docker/core/compose.yml --env-file docker/core/glados.env up -d

Образ собирается сам (или заранее: `docker build -f docker/core/Dockerfile -t gits-core:dev .`). Папка персонажа монтируется в `/data`,
память лежит в именованном томе `gits-<id>-memory` на `/data/memory` (SQLite WAL на bind-mount Windows ненадёжен). Порт опубликован
только на `127.0.0.1:${CORE_PORT}`; дашборд: `http://127.0.0.1:8766/`.

Участники на хосте: `GITS_CORE_URL=ws://127.0.0.1:<CORE_PORT>/ws` (прежний путь `/module/ws` тоже работает) и тот же `GITS_TOKEN`, что в `.env`
ядра (SDK берёт переменную раньше файла токена; прежнее имя `GITS_MODULE_TOKEN` читается как алиас). Без `GITS_TOKEN` ядро создаст токен и положит в `/data/memory/module_token`.
Сервисы хоста (decider, LM Studio, OBS, RimWorld) ядро находит по `host.docker.internal`; адреса меняются переменными
`GITS_DECIDER_URL`, `GITS_LM_URL`, `OBS_WEBSOCKET_URL`, `RIMWORLD_API`.

## Монтаж (необязательно, профиль `montage`)
Монтаж (`modules/montage`) это пространство: целый контейнер `gits-<id>-montage` (образ `gits-montage`, `docker/montage/Dockerfile`):
Python 3.12, Node 20, ffmpeg/ffprobe, шрифты, chrome-headless-shell для Remotion (скачан при сборке `npx remotion browser ensure`),
зависимости шаблона (`npm ci`) и среда агента pi. Агент берёт его в аренду и возвращает, как семафор:

- при старте контейнер к ядру НЕ подключается: поднимает интерфейс управления на порту 8780 (только сеть compose, на хост не
  публикуется) и ждёт: `GET /status` (свободно или занято, кем), `GET /info` (для чего монтаж и когда в него переходить: ядро читает
  его до аренды), `POST /acquire`, `POST /release`;
- вход агента в окружение (`workspace.switch`) берёт аренду: ядро отдаёт монтажу адрес шины (`GITS_CORE_PUBLIC_URL` ядра, по
  умолчанию `ws://<id>-core:8766/ws`) и её токен, монтаж подключается и видит только ресурсы этого персонажа
  (`/characters/<id>/montage`, `GITS_CHARACTERS_ROOT=/characters`; готовые ролики в его `montage/out`, задачи в `/work/jobs/<id>`);
- занято другим персонажем: агент получает «занято: кто, с какого времени, цель»;
- у аренды нет срока и нет продления. Она кончается, когда агент выходит из окружения (`release`) или когда обрывается WebSocket
  монтажа с ядром (ядро упало или отключило участника): тогда монтаж сам отпускает аренду, отменяет идущие задачи и отключается.
  (Ограничение: задачи монтажа не переживают возврат аренды, они отменяются; агент ставит их, пока в окружении.)

Токен управления `GITS_CONTROL_TOKEN` задаётся в `.env` хоста (в `.env.example` строка-образец, пустая; любой длинный случайный
текст, например `python -c "import secrets; print(secrets.token_hex(32))"`). Его читают и ядро (через `env_file`), и контейнер
монтажа (`${GITS_CONTROL_TOKEN}` в compose). Без него сервис монтажа не стартует в режиме аренды, а ядро пишет «аренда отключена»:
монтаж недоступен. Супервизор ядра контейнер не запускает (в `peers.container.yaml` запись `montage` с `external: true` и
`control_url: http://montage:8780`): запускает compose.

Запуск (токены берутся из `.env`, поэтому он первым `--env-file`):

    docker compose --env-file .env --env-file docker/core/glados.env -f docker/core/compose.yml --profile montage up -d
    docker logs -f gits-glados-montage

Без `--profile montage` `up` монтаж не поднимает. Остановка: `... --profile montage stop montage` (ядро продолжает работать).
Образ собирается сам или заранее: `docker build -f docker/montage/Dockerfile -t gits-montage:dev .`. Контекст сборки фильтрует
`docker/montage/Dockerfile.dockerignore` (в образ идут только `sdk`, `services/agent-runtime`, `modules/montage` без `.venv`, `node_modules`,
`jobs`, секретов).

Данные: ресурсы персонажа (шрифты, звуки, стиль) монтаж берёт из тома хозяина аренды: `${CHARACTER_DIR}` монтируется в
`/characters/${CHARACTER_ID}`, значит `/characters/<id>/montage` (`resources/<Имя>/montage` на хосте); других персонажей контейнер не видит.
Готовые ролики лежат в `montage/out` этой папки (в `.gitignore`). Рабочая память модуля (папки задач `jobs/<id>/`, обучающий журнал)
лежит в именованном томе `gits-<id>-montage-work` на `/work` (`MEMORY_DIR=/work`, `MONTAGE_JOBS_DIR=/work/jobs`), не в слое контейнера.

Вход pi по подписке: `auth.json` в образ не копируется. В `compose.yml` секция закомментирована: раскомментировать `GITS_PI_AUTH` и том
`${GITS_PI_AUTH_FILE}:/run/secrets/pi_auth.json:ro` и задать `GITS_PI_AUTH_FILE` (путь к файлу на хосте; значения по умолчанию нет). Секреты `.env` монтажу не передаются.
Исходники: запрос `start` принимает пути внутри контейнера, поэтому файлы кладут в папку, смонтированную в контейнер (пример `/inbox` закомментирован в compose).

Chrome внутри контейнера работает от пользователя `gits` (uid 1000, не root). Если рендер Remotion жалуется на песочницу, проверьте
`docker logs`; Remotion запускает chrome-headless-shell сам с нужными флагами.

## Остановка и журнал

    docker compose -f docker/core/compose.yml --env-file docker/core/glados.env down     # том памяти остаётся
    docker logs -f glados-core
    docker ps --filter name=glados-core       # статус и healthcheck

## Память в том и обратно
Положить память персонажа в том (если том не пуст, нужен `-Force`; ядро лучше остановить):

    .\docker\core\seed-memory.ps1 -CharacterId glados

Скрипт собирает память через `python -m tools.character_home export` во временную папку и копирует её в том временным контейнером.
Выгрузить из тома на хост (ядро остановить):

    .\docker\core\seed-memory.ps1 -CharacterId glados -Export D:\backup\glados-memory

## Ограничения
`POST /api/notify` принимает только петлю внутри контейнера, поэтому с хоста через порт не работает. Запись в дашборд проверяет Origin
(`localhost`), чтение и WebSocket модулей работают.
