# Ядро GitS в контейнере

Один контейнер на персонажа. В контейнере только ядро и модули без железа (`telegram`, `twitch`, `obs`, файл `agent/modules.container.yaml`).
Голос, питомец и «Компьютер» остаются на хосте и подключаются к ядру по WebSocket на опубликованный порт.

## Запуск
Из корня репозитория (секреты и `GITS_MODULE_TOKEN` берутся из `.env` хоста, параметры из `docker/core/glados.env`):

    docker compose -f docker/core/compose.yml --env-file docker/core/glados.env up -d

Образ собирается сам (или заранее: `docker build -f docker/core/Dockerfile -t gits-core:dev .`). Папка персонажа монтируется в `/data`,
память лежит в именованном томе `gits-<id>-memory` на `/data/memory` (SQLite WAL на bind-mount Windows ненадёжен). Порт опубликован
только на `127.0.0.1:${CORE_PORT}`; дашборд: `http://127.0.0.1:8766/`.

Модули на хосте: `GITS_CORE_URL=ws://127.0.0.1:<CORE_PORT>/module/ws` и тот же `GITS_MODULE_TOKEN`, что в `.env` ядра (SDK берёт переменную
раньше файла токена). Без `GITS_MODULE_TOKEN` ядро создаст токен и положит в `/data/memory/module_token`.
Сервисы хоста (decider, LM Studio, OBS, RimWorld) ядро находит по `host.docker.internal`; адреса меняются переменными
`GITS_DECIDER_URL`, `GITS_LM_URL`, `OBS_WEBSOCKET_URL`, `RIMWORLD_API`.

## Монтаж (необязательно, профиль `montage`)
Модуль монтажа (`modules/montage`) работает в отдельном контейнере `gits-<id>-montage` (образ `gits-montage`, `docker/montage/Dockerfile`):
Python 3.12, Node 20, ffmpeg/ffprobe, шрифты, chrome-headless-shell для Remotion (скачан при сборке `npx remotion browser ensure`),
зависимости шаблона (`npm ci`) и среда агента pi. Он сам подключается к ядру по WebSocket `ws://core:8766/module/ws`
(`GITS_CORE_URL`, имя `core` даёт сеть compose; шина пускает его по `GITS_HUB_ALLOW`: по умолчанию 172.16.0.0/12, 192.168.0.0/16 и 10.0.0.0/8
покрывают подсеть compose). Супервизор ядра его не запускает (в `modules.container.yaml` его нет): запускает compose.

Запуск (токен берётся из `.env`, поэтому он первым `--env-file`; без токена ядро создаёт свой, и модуль к нему не подключится):

    docker compose --env-file .env --env-file docker/core/glados.env -f docker/core/compose.yml --profile montage up -d
    docker logs -f gits-glados-montage

Без `--profile montage` `up` монтаж не поднимает. Остановка: `... --profile montage stop montage` (ядро продолжает работать).
Образ собирается сам или заранее: `docker build -f docker/montage/Dockerfile -t gits-montage:dev .`. Контекст сборки фильтрует
`docker/montage/Dockerfile.dockerignore` (в образ идут только `sdk`, `services/agent-runtime`, `modules/montage` без `.venv`, `node_modules`,
`jobs`, секретов).

Данные: ресурсы персонажа (шрифты, звуки, стиль) модуль берёт из тома персонажа: `CHARACTER_HOME=/data`, то есть `/data/montage`
(`resources/<Имя>/montage` на хосте). Готовые ролики `MONTAGE_OUT_DIR=/data/montage/out` (на хосте `resources/<Имя>/montage/out`, в `.gitignore`);
настройка `output_dir` модуля по умолчанию равна этой переменной. Рабочая память модуля (папки задач `jobs/`, обучающий журнал) лежит в именованном томе
`gits-<id>-montage-work` на `/work` (`MEMORY_DIR=/work`, `MONTAGE_JOBS_DIR=/work/jobs`), не в слое контейнера.

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
