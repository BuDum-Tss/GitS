# f5-tts: сервис синтеза речи ESpeech-TTS-1

Русский F5-TTS ([ESpeech-TTS-1 SFT-256K](https://huggingface.co/ESpeech/ESpeech-TTS-1_SFT-256K), Apache-2.0) с клонированием
голоса по референсу. Им пользуется модуль голоса (`services/voice/tts_engine.py`, цепочка ESpeech → Fish Speech → edge-tts,
переменная `TTS_ENGINE`). Отдельный процесс нужен потому, что ядро на Python 3.14, а модели нужен CUDA-torch.

Адрес: `http://127.0.0.1:8771` (`F5_PORT` меняет порт, `ESPEECH_URL` у клиента). `GET /health` отвечает 503, пока модель
грузится (около 15 с), потом 200. `POST /tts` принимает JSON `{text, ref_audio_b64 или ref_audio, ref_text}` и отдаёт WAV (24 кГц).
Ударения расставляет RUAccent; `+` перед ударной гласной в тексте сохраняется.

## Установка

```powershell
py -3.12 -m venv services\f5-tts\.venv
services\f5-tts\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
services\f5-tts\.venv\Scripts\python.exe -m pip install f5-tts ruaccent huggingface_hub soundfile
```

Веса (2,5 ГБ) кладутся в `services/f5-tts/weights/espeech/` (`espeech_tts_256k.pt`, `vocab.txt`):

```powershell
services\f5-tts\.venv\Scripts\python.exe -c "from huggingface_hub import hf_hub_download as d; [d('ESpeech/ESpeech-TTS-1_SFT-256K', f, local_dir='services/f5-tts/weights/espeech') for f in ('espeech_tts_256k.pt','vocab.txt')]"
```

`.venv/`, `weights/` и `out/` в git не входят.

## Референс голоса

Лежит в папке `voice/` персонажа: `espeech_ref.wav` (6-10 с чистой речи, обрезанной по паузе) и `espeech_ref.txt` (дословный текст
записи, его требует F5-TTS). Нет файлов - ESpeech не используется, работает Fish Speech.

## Запуск и настройки

Обычно запускает супервизор ядра (запись `espeech` в `agent/peers.yaml`, `kind: service`). Вручную:
`services\f5-tts\.venv\Scripts\python.exe services\f5-tts\f5_server.py`. `F5_NFE_STEP` (по умолчанию 16; больше шагов - лучше
качество и медленнее), `F5_DEVICE` (`cuda` или `cpu`), `F5_HOST` (адрес, по умолчанию 127.0.0.1), `F5_CKPT`, `F5_VOCAB`,
`F5_WARMUP_DIR` (папка с `espeech_ref.wav` и `espeech_ref.txt` для прогрева), `F5_ACCENT_DIR` (куда RUAccent кладёт модели),
`F5_REF_CACHE` (кэш референсов из `ref_audio_b64`).

## Контейнер `espeech-tts`

Тот же сервис в Docker (образ `espeech-tts:latest`, `docker/espeech/Dockerfile`; нужен NVIDIA Container Toolkit): ядру в контейнере
не запустить процесс хоста, поэтому там сервис ставится рядом.

```powershell
docker compose up -d espeech      # порт 127.0.0.1:8771, как у процесса из .venv
```

- В `.env` поставь `ESPEECH_EXTERNAL=1`: супервизор ядра тогда не запускает процесс из .venv (второй на том же порту).
- Веса берутся с хоста: `services/f5-tts/weights/` (`ESPEECH_WEIGHTS` меняет папку), в образ они не входят. Их надо скачать, как
  выше. Референс прогрева: `resources/GlaDOS/voice` только для чтения.
- Первый запуск докачивает модели RUAccent и vocos в том `espeech_cache` (нужен интернет), потом они лежат там. `/health` отвечает
  503, пока модель грузится (минуты при первом запуске).
- `ESPEECH_HOST` и `ESPEECH_PORT` меняют адрес на хосте (клиенту: `ESPEECH_URL`). Контейнер идёт без перезапуска (`restart: "no"`).
- Контейнер не видит файлов хоста, поэтому модуль голоса шлёт референс в поле `ref_audio_b64` (WAV в base64, сервер кэширует его
  по хэшу); поле `ref_audio` (путь) по-прежнему работает, для процесса на хосте и старых клиентов.
- Вариант с `.venv` остаётся рабочим: без `ESPEECH_EXTERNAL` ядро запускает его как раньше. Одновременно контейнер и процесс не
  держи: порт один.

Видеопамять: около 3,3 ГБ на модель, скорость около 1,2 от длительности звука на RTX с 6 ГБ.
