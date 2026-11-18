# Хостовый помощник RimWorld

Маленький процесс на хосте Windows для модуля RimWorld, который работает в контейнере (`docs/RIMWORLD_CONTAINER.md`, раздел «Контракт помощника»).
Он делает то, что без хоста не работает: запускает игру, держит окно, нажимает кнопки и снимает скриншоты окна, пишет видео и звук забега и
проксирует мод Agent Bridge (мод слушает только `[::1]:8765`, из контейнера он недоступен).

Модуль в контейнере зовёт его через `RemoteHostBridge` (`agent/host_bridge.py`); без `GITS_RIMWORLD_HOST_URL` модуль работает прямо на хосте и помощник не нужен.

## Запуск

```powershell
cd workspaces\rimworld\host
pip install -r requirements.txt            # плюс SDK: pip install -e <путь к GitS>\sdk
$env:GITS_RIMWORLD_HOST_TOKEN = "<длинная случайная строка>"
python main.py
```

Без `GITS_RIMWORLD_HOST_TOKEN` помощник не стартует. Слушает только `127.0.0.1:8791` (`GITS_RIMWORLD_HOST_PORT`); контейнер достаёт его по
`http://host.docker.internal:8791`. Токен в журнал не пишется; тот же токен контейнеру задаётся переменной `GITS_RIMWORLD_HOST_TOKEN`.

Пример записи для `peers.yaml` (запуск супервизором ядра; сам `peers.yaml` не менялся, это образец):

```yaml
- id: rimworld-host
  command: ["python", "-u", "main.py"]
  cwd: workspaces/rimworld/host
  env:
    GITS_RIMWORLD_HOST_TOKEN: "${GITS_RIMWORLD_HOST_TOKEN}"
```

## Переменные

| Переменная | Значение |
|---|---|
| `GITS_RIMWORLD_HOST_TOKEN` | токен (обязателен) |
| `GITS_RIMWORLD_HOST_PORT` | порт, по умолчанию 8791 |
| `RIMWORLD_API` | адрес мода для прокси `/game-api`, по умолчанию `http://localhost:8765` (запасной `http://[::1]:8765`) |
| `GITS_RIMWORLD_HOST_MEMORY` | папка данных помощника (протокол событий, видео), по умолчанию `host/memory` |
| `GITS_VIDEO_DIR`, `GITS_FFMPEG`, `GITS_RIMWORLD_EXE`, `GITS_NO_LAUNCH` | как у модуля (`GITS_NO_LAUNCH=1` запрещает запуск игры) |

## Устройство

`server.py`: aiohttp-приложение поверх `LocalHostBridge` (он вызывает `game_launcher`, `game_watchdog`, `game_ui`, `run_recorder` модуля из `../agent`).
Объявления (`announce_*`) и записи протокола событий попадают в журнал событий помощника (500 последних), модуль читает его `GET /events` и отдаёт
тем же обработчикам, что и в локальном режиме. UI-действия работают только по окну RimWorld (`game_ui` ищет его по заголовку), точки вне окна отклоняются.

Тесты: `host/tests/test_host_helper.py` (без игры и Windows-API).
