# Сервис распознавания речи stt

Отдельный общий контейнер для всех персонажей (как `decider` и `espeech`): распознаёт русскую речь на процессоре. Им пользуется движок
голоса ядра (`agent/services/voice_engine`, клиент `HttpSttClient`, см. `docs/VOICE_ENGINE.md`).

- Основной движок: GigaAM v3 (`gigaam-v3-e2e-rnnt`, onnx, int8, через `onnx-asr`), только русский, около 0,1 с на секунду речи.
- Запасной: faster-whisper (int8), модель `small`. Подхватывает, если GigaAM не открылся (нет весов или пакета).
- Модель открывается ОДИН раз в фоне сразу после старта (сервер отвечает сразу, `/health` даёт 503, пока модель не загружена), затем прогрев
  секундой тишины. Нет весов: открытие повторяется каждые `STT_RETRY_S` секунд, поэтому после загрузки весов перезапуск не нужен.
- Токена нет: сеть compose внутренняя. Порт 8772 на хост НЕ публикуется (для отладки добавь в `docker-compose.yml` `ports: ["127.0.0.1:8772:8772"]`).
- Текст реплик в журнал не пишется (только длина звука и время).

## Устройство
Пакет `services/stt/stt/` (в образе `/opt/stt/stt`): `main.py` (запуск, `python -m stt.main`), `app.py` (aiohttp), `engine.py`
(`Engine`, `GigaAmEngine`, `WhisperEngine`, `FallbackEngine`, `FakeEngine` для тестов, фильтр мусора, PCM в float), `config.py`
(переменные), `fetch.py` (загрузка весов). Распознавание блокирующее: идёт в пуле потоков `STT_WORKERS`, ещё `STT_QUEUE` запросов ждут,
остальные получают 503 с `Retry-After`. Таймаут `STT_TIMEOUT_S` включает ожидание в очереди (504); занятый поток освобождается, когда он
действительно закончил, даже если запрос уже отвалился.

Дублирование: фильтр мусорных фраз, пороги Whisper и поиск весов GigaAM скопированы из `services/voice/local_stt.py` (пакеты независимы).
Копия в `services/voice` уйдёт, когда голос ПК перейдёт на этот сервис.

## API
- `POST /v1/transcribe?lang=ru`: тело сырой PCM s16le 16 кГц моно (`application/octet-stream`), 0,2..60 с. Ответ 200
  `{"text", "language", "duration_ms", "engine", "took_ms"}`. Ошибки (тело `{"error", "detail"}`): 400 мусор (пусто, нечётная длина,
  короче 0,2 с, неверный код языка, язык не поддержан движком: GigaAM только `ru`), 413 длиннее 60 с, 503 очередь полна или модель не
  загружена (`Retry-After`), 504 таймаут, 500 движок упал. Параметр `lang` по умолчанию `GITS_STT_LANGUAGE`.
- `GET /health`: 200 `{"ok": true, "engine", "model_loaded": true, "queue"}` или 503, пока модель не загружена (для healthcheck compose).
- `GET /v1/info`: движок, модель, язык, потоки, воркеры, лимиты, текущая загрузка.

## Запуск
Из корня репозитория: `docker compose up -d stt` (образ `gits-stt:latest`, контейнер `stt`, `restart: unless-stopped`, память `STT_MEM_LIMIT`
по умолчанию 2g). Ядро находит его по `GITS_STT_URL=http://stt:8772` (задан по умолчанию в `docker/core/compose.yml`). Проверка из
контейнера ядра или сети compose: `curl http://stt:8772/health`. Без Docker (на хосте): `cd services/stt && python -m stt.main`
(зависимости: `pip install -r docker/stt/requirements.txt`, веса из `~/.cache/huggingface`, порт `STT_PORT` 8772, адрес `STT_HOST` 127.0.0.1).

## Переменные окружения
| Переменная | По умолчанию | Смысл |
|---|---|---|
| `GITS_STT_ENGINE` | `gigaam` | `gigaam` (с запасным whisper) или `whisper` сразу |
| `GITS_STT_GIGAAM` | `gigaam-v3-e2e-rnnt` | модель GigaAM (имя `onnx-asr`) |
| `GITS_STT_GIGAAM_REPO` | `istupakov/gigaam-v3-onnx` | репозиторий Hugging Face с весами GigaAM |
| `GITS_STT_MODEL` | `small` | модель faster-whisper (репозиторий `Systran/faster-whisper-<имя>` или полное имя) |
| `GITS_STT_LANGUAGE` | `ru` | язык по умолчанию |
| `GITS_STT_THREADS` | `2` | потоков процессора на одно распознавание |
| `STT_WORKERS` | `2` | распознаваний одновременно (пул потоков) |
| `STT_QUEUE` | `8` | ожидающих сверх воркеров; больше: 503 |
| `STT_TIMEOUT_S` | `30` | таймаут запроса вместе с очередью |
| `STT_RETRY_S` | `60` | повтор открытия модели, если весов нет |
| `STT_WARMUP` | `1` | прогрев после загрузки |
| `STT_HOST`, `STT_PORT` | `127.0.0.1` (в образе `0.0.0.0`), `8772` | адрес сервера |
| `HF_HOME_HOST` | `~/.cache/huggingface` | КАКОЙ кэш Hugging Face монтировать в контейнер (параметр compose, задаётся в `.env`) |
| `STT_MEM_LIMIT` | `2g` | предел памяти контейнера |

Процессоров много на запрос не нужно: полная нагрузка `STT_WORKERS` x `GITS_STT_THREADS` потоков. Игре и LM Studio оставь запас.

## Веса моделей
Сервис ничего не скачивает (`HF_HUB_OFFLINE=1`), только читает кэш Hugging Face. Корень кэша в контейнере `HF_HOME=/models/hf`, модели лежат
в `/models/hf/hub/models--<владелец>--<имя>/snapshots/<ревизия>/`. Контейнер читает кэш только для чтения и ничего в него не пишет
(временные файлы в `/tmp`).

Нужные файлы:
- GigaAM, репозиторий `istupakov/gigaam-v3-onnx`: `config.json`, `v3_e2e_rnnt_encoder.int8.onnx` (около 215 МБ),
  `v3_e2e_rnnt_decoder.int8.onnx`, `v3_e2e_rnnt_joint.int8.onnx`, `v3_e2e_rnnt_vocab.txt`;
- запасной Whisper, репозиторий `Systran/faster-whisper-small`: `config.json`, `model.bin` (около 460 МБ), `tokenizer.json`, `vocabulary.txt`
  (при `GITS_STT_MODEL=base|medium|tiny` соответствующий репозиторий `Systran/faster-whisper-<имя>`).

### Способ 1 (по умолчанию): готовый кэш хоста
Если GigaAM и Whisper уже скачивались на хосте (их использует голос ПК), ничего делать не нужно: compose монтирует `~/.cache/huggingface`
хоста в `/models/hf` только для чтения. Другой каталог: `HF_HOME_HOST=D:/hf-cache` в `.env` (каталог, внутри которого лежит `hub/`).
Если `~` в вашем окружении не раскрылся, укажите путь явно, например `HF_HOME_HOST=C:/Users/<имя>/.cache/huggingface`.
Проверить, что нужное есть (без сети, на хосте): `cd services/stt && python -m stt.fetch --check`.

### Способ 2 (запасной): скачать в том `stt_models`
Если кэша нет, разовая команда из корня репозитория (нужна сеть: при VPN запускай её с включённым VPN):

    docker compose --profile fetch run --rm stt-fetch

Она качает GigaAM и Whisper в именованный том `stt_models` (`gits-stt-models`). Затем в `.env` задайте `HF_HOME_HOST=stt_models`
(имя тома вместо пути) и поднимите сервис: `docker compose up -d stt`. Качать одну модель: `docker compose --profile fetch run --rm stt-fetch python -m stt.fetch gigaam`
(или `whisper`). Скачанное повторно не качается: команду безопасно повторять.

### Нет доступа к сети (VPN)
- Hugging Face недоступен: включи VPN на время загрузки; либо скачай файлы вручную (браузером или `huggingface-cli`) на другой машине и
  перенеси папки `models--istupakov--gigaam-v3-onnx` и `models--Systran--faster-whisper-small` целиком в `<кэш>/hub/`;
- команде нужен прокси: раскомментируй `HTTPS_PROXY` в сервисе `stt-fetch` (`docker-compose.yml`);
- прерванная загрузка продолжается при повторном запуске;
- без весов сервис живёт: `/health` отвечает 503 с причиной, ядро получает 503 на запросы, голос деградирует без падения; как только
  веса появятся в кэше, сервис откроет модель сам (до `STT_RETRY_S` секунд, перезапуск не нужен).

## Тесты
Без весов и сети, на `FakeEngine`: `python -m pytest -q services/stt/tests -p no:cacheprovider`. `test_stt_compat.py` поднимает приложение на
локальном порту и ходит в него настоящим `HttpSttClient` из `agent/services/voice_engine/stt.py`.

## Образ
`docker/stt/Dockerfile`: python:3.12-slim, только процессор, без torch (GigaAM идёт как onnx), пользователь `app` (uid 1000), HEALTHCHECK на `/health`.
Зависимости: `docker/stt/requirements.txt` (aiohttp, numpy, onnxruntime, onnx-asr, huggingface_hub, faster-whisper с ctranslate2, av и tokenizers).
Ожидаемый размер образа: около 0,5 ГБ (слой Python и колёс: onnxruntime 24 МБ, ctranslate2 40 МБ, av 35 МБ, numpy 17 МБ, остальное мелочь).
Оперативная память с загруженными GigaAM и Whisper: порядка 1 ГБ, предел 2 ГБ с запасом.
