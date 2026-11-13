"""Контейнер decider: общий сервис в корневом docker-compose.yml, Dockerfile, адрес ядра и skip_when_env (разбор файлов, Docker не нужен)."""
from pathlib import Path

import yaml

import supervisor

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
CORE_COMPOSE = ROOT / "docker" / "core" / "compose.yml"
DOCKERFILE = ROOT / "docker" / "decider" / "Dockerfile"


def _svc() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["decider"]


def test_decider_service_in_root_compose():
    svc = _svc()
    assert svc["container_name"] == "decider" and svc["image"] == "gits-decider:latest"
    assert svc["build"]["dockerfile"] == "docker/decider/Dockerfile" and svc["restart"] == "no"
    assert svc["ports"] == ["${DECIDER_HOST:-127.0.0.1}:${DECIDER_PORT:-8768}:8768"]
    assert any(v.endswith(":/opt/decider/hf:ro") and "services/decider/hf" in v for v in svc["volumes"])
    assert "deploy" not in svc                                          # без GPU
    test = svc["healthcheck"]["test"]
    assert test[0] == "CMD" and test[1] == "python" and "/health" in test[-1] and "urllib" in test[-1]


def test_core_reaches_decider_by_container_name():
    core = yaml.safe_load(CORE_COMPOSE.read_text(encoding="utf-8"))
    env = next(iter(core["services"].values()))["environment"]
    assert env["GITS_DECIDER_URL"] == "${GITS_DECIDER_URL:-http://decider:8768}"
    assert core.get("name", "gits") == "gits"                           # одна группа и сеть gits_default с корневым compose


def test_decider_peer_skipped_when_external(monkeypatch):
    spec = {s.id: s for s in supervisor.load_specs(ROOT / "agent" / "peers.yaml")}["decider"]
    assert spec.skip_when_env == {"DECIDER_EXTERNAL": ["1"]}
    assert spec.health_url == "http://127.0.0.1:8768/health"
    container = yaml.safe_load((ROOT / "agent" / "peers.container.yaml").read_text(encoding="utf-8"))
    assert "decider" not in {m["id"] for m in container.get("workspaces", []) + (container.get("services") or [])}


def test_dockerfile_non_root_cpu_weights_outside_image():
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in text and "whl/cpu" in text and "requirements.lock.txt" in text
    assert "USER app" in text and "GITS_DECIDER_HOST=0.0.0.0" in text and "HF_HUB_OFFLINE=1" in text
    assert "COPY --chown=app:app services/decider/decider_server.py services/decider/envcompat.py" in text
    assert "hf " not in text.split("COPY --chown", 1)[1].splitlines()[0]   # веса не копируются
