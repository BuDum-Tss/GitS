"""Контейнер RimWorld: сервис `rimworld` в корневом docker-compose.yml, Dockerfile и запись в peers.container.yaml (разбор файлов, Docker не нужен)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "docker" / "rimworld" / "Dockerfile"
IGNORE = ROOT / "docker" / "rimworld" / "Dockerfile.dockerignore"
REQUIREMENTS = ROOT / "docker" / "rimworld" / "requirements.txt"


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_peers_container_has_external_shared_rimworld():
    peers = yaml.safe_load((ROOT / "agent" / "peers.container.yaml").read_text(encoding="utf-8"))
    entry = next(w for w in peers["workspaces"] if w["id"] == "rimworld")
    assert entry["ownership"] == "shared" and entry["external"] is True and entry["title"] == "RimWorld"
    assert entry["control_url"] == "http://rimworld:8782" and entry["control_token_env"] == "GITS_CONTROL_TOKEN"
    assert "command" not in entry and "autostart" not in entry              # супервизор контейнер не запускает
    host = yaml.safe_load((ROOT / "agent" / "peers.yaml").read_text(encoding="utf-8"))
    local = next(w for w in host["workspaces"] if w["id"] == "rimworld")
    assert local["autostart"] is True and "external" not in local           # режим «ядро на хосте»: прежний локальный запуск


def test_compose_service_rimworld():
    compose = _compose()
    svc = compose["services"]["rimworld"]
    assert svc["container_name"] == "rimworld" and svc["restart"] == "unless-stopped"
    assert svc["build"]["dockerfile"] == "docker/rimworld/Dockerfile" and svc["image"].startswith("${RIMWORLD_IMAGE:-gits-rimworld:dev")
    env = svc["environment"]
    assert env["GITS_CONTROL_TOKEN"].startswith("${GITS_CONTROL_TOKEN") and env["GITS_CONTROL_PORT"] == "8782"
    assert "RIMWORLD_API" not in env                                        # мод достижим только через прокси помощника (порт 8765 занят, мод на IPv6)
    assert env["GITS_RIMWORLD_HOST_URL"].startswith("${GITS_RIMWORLD_HOST_URL:-http://host.docker.internal:8791")
    assert env["GITS_RIMWORLD_HOST_TOKEN"].startswith("${GITS_RIMWORLD_HOST_TOKEN")
    assert env["RIMWORLD_MEMORY_DIR"] == "/work/rimworld" and env["GITS_DASHBOARD_HOST"] == "0.0.0.0"
    assert "host.docker.internal:host-gateway" in svc["extra_hosts"]
    assert "8782" in [str(p) for p in svc["expose"]]
    assert svc["ports"] == ["127.0.0.1:8767:8767"]                          # порт управления не публикуется, оверлей только на петлю
    assert "/status" in " ".join(svc["healthcheck"]["test"])
    assert "rimworld_memory:/work/rimworld" in svc["volumes"] and "./resources/GlaDOS:/characters/glados" in svc["volumes"]
    assert "rimworld_memory" in compose["volumes"]
    assert "env_file" not in svc and "depends_on" not in svc                # секреты .env не передаются, к ядру при старте не подключается
    for secret in ("GITS_TOKEN", "GITS_CORE_URL", "ANTHROPIC_API_KEY"):
        assert secret not in env


def test_dockerfile_and_requirements_without_windows_packages():
    text = DOCKERFILE.read_text(encoding="utf-8")
    for needle in ("python:3.12-slim", "USER gits", "GITS_RIMWORLD_CONTAINER=1", "HEALTHCHECK", "/status", "workspaces/rimworld",
                   "agent/interfaces/workspace", '"main.py"'):
        assert needle in text, needle
    lines = REQUIREMENTS.read_text(encoding="utf-8").lower().splitlines()
    reqs = chr(10).join(ln for ln in lines if not ln.startswith("#"))
    for windows in ("pyautogui", "pywin32", "pygetwindow", "psutil", "soundcard"):
        assert windows not in reqs, windows
    for needed in ("aiohttp", "pydantic", "httpx", "python-dotenv", "mcp"):
        assert needed in reqs, needed


def test_dockerignore_keeps_secrets_and_memory_out():
    lines = [ln.strip() for ln in IGNORE.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "*" and "!workspaces/rimworld" in lines and "!agent/interfaces/workspace" in lines
    for needed in ("**/.env", "workspaces/rimworld/agent/memory", "workspaces/rimworld/mod"):
        assert needed in lines, needed
    assert "!resources" not in lines and "!agent" not in lines
