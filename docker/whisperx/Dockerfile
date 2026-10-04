FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git \
    curl \
    ffmpeg \
    python3 \
    python3-pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/whisperx

# whisperx требует torch~=2.8.0 (жёстче, чем у fish-speech) — не пиновать
# отдельно, иначе pip сначала ставит один torch, потом сносит его ради
# зависимости whisperx и качает второй раз. Проверено: torch 2.8.0 из PyPI
# тянет nvidia-cuda-runtime-cu12 12.8.x — Blackwell (sm_120) по-прежнему
# поддержан.
RUN pip3 install --no-cache-dir --break-system-packages \
    whisperx \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

COPY docker/whisperx/server.py server.py

EXPOSE 9000

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "9000"]
