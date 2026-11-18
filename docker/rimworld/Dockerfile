# Окружение RimWorld GitS в контейнере (образ gits-rimworld): общее пространство, которое ядро берёт в аренду (как монтаж).
# Сборка из корня репозитория: docker build -f docker/rimworld/Dockerfile -t gits-rimworld:dev .
# Контекст фильтрует docker/rimworld/Dockerfile.dockerignore (корневой .dockerignore для этого образа не используется).
# В образе только код модуля и SDK: игра, окно, ввод и запись идут через хостовый помощник (GITS_RIMWORLD_HOST_URL), Windows-пакетов
# и секретов .env здесь нет. Дизайн: docs/RIMWORLD_CONTAINER.md.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/gits \
    PYTHONPATH=/app/agent/interfaces/workspace \
    GITS_RIMWORLD_CONTAINER=1 \
    GITS_CONTROL_PORT=8782 \
    RIMWORLD_MEMORY_DIR=/work/rimworld

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin gits \
    && mkdir -p /app /data /work/rimworld \
    && chown -R gits:gits /app /data /work

WORKDIR /app

# зависимости Python отдельным слоем
COPY docker/rimworld/requirements.txt docker/rimworld/requirements.txt
RUN pip install -r docker/rimworld/requirements.txt

USER gits
COPY --chown=gits:gits agent/interfaces/workspace agent/interfaces/workspace
COPY --chown=gits:gits workspaces/rimworld workspaces/rimworld

VOLUME ["/work"]
WORKDIR /app/workspaces/rimworld/agent
EXPOSE 8782 8767
HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:' + os.environ.get('GITS_CONTROL_PORT', '8782') + '/status', headers={'Authorization': 'Bearer ' + os.environ['GITS_CONTROL_TOKEN']}), timeout=3)"
CMD ["python", "-u", "main.py"]
