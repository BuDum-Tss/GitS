# Ядро GitS в контейнере: один контейнер на персонажа (том персонажа в /data, см. docker/core/README.md).
# Сборка из корня репозитория: docker build -f docker/core/Dockerfile -t gits-core:dev .
# В образе нет GPU, звука и окон: голос, питомец и «Компьютер» остаются на хосте и подключаются к ядру по WebSocket.

# --- зависимости среды pi (Node 20) ---
FROM node:20-slim AS agent-runtime
WORKDIR /app/services/agent-runtime
COPY services/agent-runtime/package.json services/agent-runtime/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

# --- ядро ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/gits \
    GITS_CONTAINER=1 \
    CHARACTER_HOME=/data \
    MEMORY_DIR=/data/memory \
    GITS_DASHBOARD_HOST=0.0.0.0 \
    GITS_PEERS_FILE=agent/peers.container.yaml

# Node 20 для среды pi: бинарник из образа node (npm в рантайме не нужен)
COPY --from=agent-runtime /usr/local/bin/node /usr/local/bin/node

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin gits \
    && mkdir -p /app /data/memory \
    && chown gits:gits /app /data /data/memory

WORKDIR /app

# зависимости ядра и модулей без железа (telegram, twitch, obs): отдельным слоем, меняются редко
COPY agent/requirements.txt agent/requirements.txt
COPY docker/core/requirements.txt docker/core/requirements.txt
RUN pip install -r docker/core/requirements.txt

COPY --from=agent-runtime /app/services/agent-runtime/node_modules services/agent-runtime/node_modules
COPY sdk sdk
COPY services/agent-runtime services/agent-runtime
COPY agent agent
COPY workspaces/telegram workspaces/telegram
COPY workspaces/twitch workspaces/twitch
COPY workspaces/obs workspaces/obs

RUN mkdir -p agent/logs && chown -R gits:gits agent/logs
USER gits

VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/api/state', timeout=4).status == 200 else 1)"

WORKDIR /app/agent
CMD ["python", "main.py"]
