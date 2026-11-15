# Ядро GitS в контейнере

Один контейнер на персонажа. В контейнере только ядро и окружения без железа (`telegram`, `discord` и `streamer` (Twitch и OBS одним окружением), все со владением `owned`; файл `agent/peers.container.yaml`,
его выбирает `GITS_PEERS_FILE`).
Голос, питомец и «Компьютер» остаются на хосте и подключаются к ядру по WebSocket на опубликованный порт.

## Запуск
Из корня репозитория (секреты и `GITS_TOKEN` берутся из `.env` хоста, параметры из `docker/core/glados.env`):

    docker compose -f docker/core/compose.yml --env-file docker/core/glados.env up -d

Образ собирается сам (или заранее: `docker build -f docker/core/Dockerfile -t gits-core:dev .`). Папка персонажа монтируется в `/data`,
память лежит в именованном томе `gits-<id>-memory` на `/data/memory` (SQLite WAL на bind-mount Windows ненадёжен). Порт опубликован
только на `127.0.0.1:${CORE_PORT}`; дашборд: `http://127.0.0.1:8766/`.

Участники на хосте: `GITS_CORE_URL=ws://127.0.0.1:<CORE_PORT>/ws` и тот же `GITS_TOKEN`, что в `.env`
ядра (SDK берёт переменную раньше файла токена). Без `GITS_TOKEN` ядро создаст токен и положит в `/data/memory/module_token`.
Классификатор decider это общий контейнер `decider` (корневой `docker-compose.yml`, группа gits, сеть `gits_default` общая с ядром):
ядро ходит к нему по `GITS_DECIDER_URL=http://decider:8768` (запуск из корня: `docker compose up -d decider`). Остальные сервисы хоста (LM Studio, OBS,
RimWorld) ядро находит по `host.docker.internal`; адреса меняются переменными `GITS_DECIDER_URL`, `GITS_LM_URL`, `OBS_WEBSOCKET_URL`, `RIMWORLD_API`.

## Монтаж (необязательно, общий контейнер `montage`)
Монтаж (`workspaces/montage`) это пространство: ОДИН общий контейнер `montage` на всех персонажей (образ `gits-montage:dev`, `docker/montage/Dockerfile`; описан в корневом `docker-compose.yml` рядом с espeech, а не в compose ядра):
Python 3.12, Node 22, ffmpeg/ffprobe, шрифты, chrome-headless-shell для Remotion (скачан при сборке `npx remotion browser ensure`),
зависимости шаблона (`npm ci`); своих агентов окружение не запускает, монтирует сама ГЛаДОС. Агент берёт его в аренду и возвращает, как семафор:

- при старте контейнер к ядру НЕ подключается: поднимает интерфейс управления на порту 8780 (только сеть compose, на хост не
  публикуется) и ждёт: `GET /status` (свободно или занято, кем), `GET /info` (для чего монтаж и когда в него переходить: ядро читает
  его до аренды), `POST /acquire`, `POST /release`;
- вход агента в окружение (`workspace.enter`) берёт аренду: ядро отдаёт монтажу адрес шины (`GITS_CORE_PUBLIC_URL` ядра, по
  умолчанию `ws://<id>-core:8766/ws`) и её токен, монтаж подключается и видит только ресурсы этого персонажа
  (`/characters/<id>/montage`, `GITS_CHARACTERS_ROOT=/characters`, у каждого персонажа своя строка тома в корневом compose; готовые ролики в его `montage/out`, задачи в его `montage/jobs`);
- занято другим персонажем: агент получает «занято: кто, с какого времени, цель»;
- у аренды нет срока и нет продления. Она кончается, когда агент выходит из окружения (`release`) или когда обрывается WebSocket
  монтажа с ядром (ядро упало или отключило участника): тогда монтаж сам отпускает аренду, отменяет идущие задачи и отключается.
- выход агента не отменяет задачи: пока у монтажа есть активные задачи, аренда остаётся за хозяином («моё, дорабатывает задачи (N)»,
  другим «занято»), хозяин может вернуться; когда задач не осталось, монтаж сам освобождается и отключается от ядра. Связь с ядром
  в это время остаётся, поэтому события и результаты задач доходят. Задачи, ждущие ответа агента (`waiting_user`), аренду не держат:
  они не отменяются, а паркуются на диске (папка задач хозяина) и возвращаются при следующем входе того же персонажа, вопрос задаётся снова.

Токен управления `GITS_CONTROL_TOKEN` задаётся в `.env` хоста (в `.env.example` строка-образец, пустая; любой длинный случайный
текст, например `python -c "import secrets; print(secrets.token_hex(32))"`). Его читают и ядро (через `env_file`), и контейнер
монтажа (`${GITS_CONTROL_TOKEN}` в compose). Без него сервис монтажа не стартует в режиме аренды, а ядро пишет «аренда отключена»:
монтаж недоступен. Супервизор ядра контейнер не запускает (в `peers.container.yaml` запись `montage` с `external: true` и
`control_url: http://montage:8780`): запускает compose.

Запуск из корня репозитория (`--env-file .env` нужен, чтобы подставился `GITS_CONTROL_TOKEN`; остальные секреты `.env` монтажу не передаются,
`env_file` у сервиса нет):

    docker compose --env-file .env up -d montage
    docker logs -f montage

Проект у обоих compose-файлов один (`name: gits`), поэтому ядро (`<id>-core`) и монтаж лежат в одной сети `gits_default` и видят друг друга по
имени сервиса: ядро ходит на `http://montage:8780` (`control_url` в `agent/peers.container.yaml`), монтаж подключается к `ws://<id>-core:8766/ws`.
Порт управления 8780 на хост не публикуется. Остановка: `docker compose stop montage` (ядро продолжает работать).

Новый персонаж: добавить в сервис `montage` корневого `docker-compose.yml` свою строку тома, например
`./resources/Aperture:/characters/aperture` (id в пути `/characters/<id>` равен `id` персонажа), и перезапустить `montage`
(`docker compose --env-file .env up -d montage`). Хозяин аренды видит только свою папку.

Образ собирается сам или заранее: `docker build -f docker/montage/Dockerfile -t gits-montage:dev .`. Контекст сборки фильтрует
`docker/montage/Dockerfile.dockerignore` (в образ идут только `sdk` и `workspaces/montage` без `.venv`, `node_modules`,
`jobs`, секретов).

Данные: ресурсы персонажа (шрифты, звуки, стиль) монтаж берёт из тома хозяина аренды: папка персонажа монтируется в
`/characters/<id>`, значит `/characters/<id>/montage` (`resources/<Имя>/montage` на хосте); других персонажей контейнер не видит.
Готовые ролики лежат в `montage/out` этой папки (в `.gitignore`). Рабочая память участника шины (папки задач `jobs/<id>/`, обучающий журнал)
хранится в папке персонажа `montage/jobs`. Том `gits-montage-work` используется для служебной памяти и может содержать старые рабочие папки в `/work/jobs`.

Секретов входа модели у монтажа нет (модель работает у ядра), секреты `.env` монтажу не передаются.
Исходники: инструмент `work` (`create`) принимает пути внутри контейнера, поэтому файлы кладут в папку, смонтированную в контейнер (пример `/inbox` закомментирован в compose).

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
(`localhost`), чтение и WebSocket участников шины работают.
