FROM node:22-bookworm-slim AS node
WORKDIR /app
COPY services/agent-runtime/package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep bash \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /app/node_modules /app/node_modules
RUN pip install pytest pytest-asyncio ruff \
    && useradd --uid 1000 --create-home repair
ENV HOME=/home/repair PI_CODING_AGENT_DIR=/tmp/pi \
    PYTHONPATH=/repo/agent/interfaces/workspace
RUN git config --system --add safe.directory /repo
USER repair
WORKDIR /repo
ENTRYPOINT ["node", "/app/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"]

