# Сервис распознавания речи stt (GigaAM v3 через onnx-asr, запасной faster-whisper, только процессор): services/stt. Контекст сборки:
# корень репозитория. Без torch: GigaAM идёт как onnx (int8), поэтому образ лёгкий (около 0,5 ГБ против 2 ГБ с torch).
# Веса (около 0,5 ГБ GigaAM + 0,5 ГБ Whisper small) в образ не входят: кэш Hugging Face монтируется в /models/hf (по умолчанию кэш хоста,
# только для чтения; HF_HUB_OFFLINE=1, сервис ничего не качает). Разовая загрузка весов: python -m stt.fetch (см. services/stt/README.md).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

# libgomp1: OpenMP для onnxruntime на случай, если колесо не принесло свою копию
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY docker/stt/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd --create-home --uid 1000 app && mkdir -p /models/hf && chown -R app:app /models

WORKDIR /opt/stt
COPY --chown=app:app services/stt/stt /opt/stt/stt

USER app

# HF_HOME: корень кэша, модели лежат в /models/hf/hub. Офлайн: кэш может быть смонтирован только для чтения. Временные файлы в /tmp.
ENV HF_HOME=/models/hf \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_XET=1 \
    TMPDIR=/tmp \
    STT_HOST=0.0.0.0 \
    STT_PORT=8772

EXPOSE 8772

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=5 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8772/health', timeout=3)"]

CMD ["python", "-m", "stt.main"]
