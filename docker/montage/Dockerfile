# Окружение монтажа GitS в контейнере (образ gits-montage): отдельный контейнер, подключается к ядру по WebSocket (GITS_CORE_URL).
# Сборка из корня репозитория: docker build -f docker/montage/Dockerfile -t gits-montage:dev .
# Контекст фильтрует docker/montage/Dockerfile.dockerignore (корневой .dockerignore для этого образа не используется).
# В образе: Python 3.12, Node 22, ffmpeg/ffprobe, шрифты, библиотеки headless Chrome, chrome-headless-shell для Remotion,
# зависимости шаблона (npm ci). Своих агентов окружение не запускает (монтирует сама ГЛаДОС через ядро), моста pi и секретов
# входа модели в образе нет: модель работает у ядра.

# --- источник node и npm для итогового образа (Node 22 нужен Remotion) ---
FROM node:22-slim AS node

# --- окружение ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/gits \
    PYTHONPATH=/app/agent/interfaces/workspace \
    CHARACTER_HOME=/data \
    MEMORY_DIR=/work \
    MONTAGE_JOBS_DIR=/work/jobs \
    MONTAGE_OUT_DIR=/data/montage/out

# ffmpeg/ffprobe (PATH), шрифты, библиотеки для headless Chrome (список из документации Remotion «Docker»)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates fontconfig fonts-liberation fonts-noto-color-emoji \
        libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2t64 libxrandr2 libxkbcommon-dev libxfixes3 \
        libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2 \
    && rm -rf /var/lib/apt/lists/*

# Node 22 и npm/npx (npx нужен Remotion и окружению: media.find_npx)
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin gits \
    && mkdir -p /app /data /work/jobs \
    && chown -R gits:gits /app /data /work

WORKDIR /app

# зависимости Python отдельным слоем (SDK ядра: aiohttp, он уже в requirements окружения)
COPY workspaces/montage/requirements.txt workspaces/montage/requirements.txt
RUN pip install -r workspaces/montage/requirements.txt

USER gits

# зависимости шаблона Remotion и браузер: слой меняется только вместе с package-lock.json
WORKDIR /app/workspaces/montage/template
COPY --chown=gits:gits workspaces/montage/template/package.json workspaces/montage/template/package-lock.json ./
RUN npm ci --no-audit --no-fund && npm cache clean --force \
    && npx remotion browser ensure

WORKDIR /app
COPY --chown=gits:gits agent/interfaces/workspace agent/interfaces/workspace
COPY --chown=gits:gits workspaces/montage workspaces/montage

VOLUME ["/data", "/work"]
WORKDIR /app/workspaces/montage/agent
CMD ["python", "-u", "main.py"]
