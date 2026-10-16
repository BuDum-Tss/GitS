# f5-tts: сервис синтеза речи ESpeech-TTS-1

Русский F5-TTS ([ESpeech-TTS-1 SFT-256K](https://huggingface.co/ESpeech/ESpeech-TTS-1_SFT-256K), Apache-2.0) с клонированием
голоса по референсу. Им пользуется модуль голоса (`modules/voice/tts_engine.py`, цепочка ESpeech → Fish Speech → edge-tts,
переменная `TTS_ENGINE`). Отдельный процесс нужен потому, что ядро на Python 3.14, а модели нужен CUDA-torch.

Адрес: `http://127.0.0.1:8771` (`F5_PORT` меняет порт, `ESPEECH_URL` у клиента). `GET /health` отвечает 503, пока модель
грузится (около 15 с), потом 200. `POST /tts` принимает JSON `{text, ref_audio, ref_text}` и отдаёт WAV (24 кГц).
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

Обычно запускает супервизор ядра (запись `espeech` в `agent/modules.yaml`, `kind: service`). Вручную:
`services\f5-tts\.venv\Scripts\python.exe services\f5-tts\f5_server.py`. `F5_NFE_STEP` (по умолчанию 32; больше шагов - лучше
качество и медленнее), `F5_DEVICE` (`cuda` или `cpu`), `F5_CKPT`, `F5_VOCAB`.

Видеопамять: около 3,3 ГБ на модель, скорость около 1,2 от длительности звука на RTX с 6 ГБ.
