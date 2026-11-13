# Сервис классификации обращений decider (GLiNER2.5-multi-Decide, процессор): services/decider/decider_server.py. Контекст сборки:
# корень репозитория. Версии как в services/decider/requirements.lock.txt (torch CPU с индекса PyTorch). Веса (около 1,1 ГБ)
# в образ не входят: том /opt/decider/hf (HF_HOME, HF_HUB_OFFLINE=1, сервер в кэш не пишет).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10

COPY services/decider/requirements.lock.txt /tmp/requirements.lock.txt
# colorama в замороженном списке нужен только на Windows, но безвреден
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/requirements.lock.txt \
 && rm /tmp/requirements.lock.txt

RUN useradd --create-home --uid 1000 app && mkdir -p /opt/decider/hf && chown -R app:app /opt/decider

WORKDIR /opt/decider
COPY --chown=app:app services/decider/decider_server.py services/decider/envcompat.py /opt/decider/

USER app

ENV HF_HOME=/opt/decider/hf \
    HF_HUB_OFFLINE=1 \
    GITS_DECIDER_HOST=0.0.0.0 \
    GITS_DECIDER_PORT=8768

EXPOSE 8768

CMD ["python", "decider_server.py"]
