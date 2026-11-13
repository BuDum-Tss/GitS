# Сервис синтеза речи ESpeech-TTS-1 (F5-TTS, русский): services/f5-tts/f5_server.py. Контекст сборки: корень репозитория.
# CUDA приходит в колёсах torch (cu124, как в services/f5-tts/README.md), поэтому базовый образ обычный python, без nvidia/cuda.
# Веса (около 2,5 ГБ) в образ не входят: том /opt/f5-tts/weights. Модели RUAccent и vocos докачиваются при первом запуске в
# /home/app/.cache (именованный том espeech_cache).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/home/app/.cache/huggingface

# libsndfile1: запасной системный soundfile; ffmpeg не нужен (референс идёт WAV)
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 \
 && rm -rf /var/lib/apt/lists/*

# Те же версии, что в рабочем окружении services/f5-tts/.venv. Сначала torch с cu124, чтобы f5-tts взял его, а не CPU-сборку с PyPI.
RUN pip install "torch==2.6.0+cu124" "torchaudio==2.6.0+cu124" --index-url https://download.pytorch.org/whl/cu124

# fastapi и uvicorn приходят зависимостями f5-tts (через gradio), но сервис их использует напрямую: ставим явно.
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
RUN pip install f5-tts ruaccent huggingface_hub soundfile fastapi uvicorn

RUN useradd --create-home --uid 1000 app \
 && mkdir -p /opt/f5-tts/weights /opt/f5-tts/references /home/app/.cache \
 && chown -R app:app /opt/f5-tts /home/app/.cache  && chown -R app:app "$(python -c 'import ruaccent,os;print(os.path.dirname(ruaccent.__file__))')"
# RUAccent всегда докачивает словарь koziev и служебную папку .cache в каталог своего пакета (остальные модели идут в F5_ACCENT_DIR):
# пакет должен быть доступен для записи пользователю app. Эта часть живёт в слое контейнера и докачивается при его пересоздании.

WORKDIR /opt/f5-tts
COPY --chown=app:app services/f5-tts/f5_server.py /opt/f5-tts/f5_server.py

USER app

ENV F5_HOST=0.0.0.0 \
    F5_PORT=8771 \
    F5_CKPT=/opt/f5-tts/weights/espeech/espeech_tts_256k.pt \
    F5_VOCAB=/opt/f5-tts/weights/espeech/vocab.txt \
    F5_WARMUP_DIR=/opt/f5-tts/references \
    F5_ACCENT_DIR=/home/app/.cache/ruaccent \
    F5_REF_CACHE=/tmp/f5_refs

EXPOSE 8771

CMD ["python", "f5_server.py"]
