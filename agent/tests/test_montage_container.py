"""Контейнер монтажа: сервис `montage` в docker/core/compose.yml, Dockerfile и .gitignore (разбор файлов, Docker не нужен)."""
import ipaddress
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "core" / "compose.yml"
DOCKERFILE = ROOT / "docker" / "montage" / "Dockerfile"
IGNORE = ROOT / "docker" / "montage" / "Dockerfile.dockerignore"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_montage_service_is_optional_profile_with_healthy_core():
    compose = _compose()
    svc = compose["services"]["montage"]
    assert svc["profiles"] == ["montage"]
    assert "profiles" not in compose["services"]["core"]                      # ядро поднимается без профиля
    assert svc["depends_on"]["core"]["condition"] == "service_healthy"
    assert svc["build"]["dockerfile"] == "docker/montage/Dockerfile"
    assert svc["container_name"] == "gits-${CHARACTER_ID:-glados}-montage"
    assert compose["name"] == "gits"                                           # вся группа контейнеров в проекте gits


def test_montage_service_environment_and_volumes():
    compose = _compose()
    svc = compose["services"]["montage"]
    env = _env(svc)
    assert env["GITS_CORE_URL"] == "ws://core:8766/module/ws"
    assert env["GITS_MODULE_ID"] == "montage"
    assert env["GITS_MODULE_TOKEN"].startswith("${GITS_MODULE_TOKEN")
    assert env["GITS_CHARACTER_ID"] == "${CHARACTER_ID:-glados}"
    assert env["CHARACTER_HOME"] == "/data"
    assert env["MONTAGE_OUT_DIR"] == "/data/montage/out"
    assert env["MEMORY_DIR"] == "/work" and env["MONTAGE_JOBS_DIR"].startswith("/work/")
    volumes = svc["volumes"]
    core_bind = compose["services"]["core"]["volumes"][0]
    assert core_bind in volumes                                               # тот же bind-том персонажа
    assert core_bind.endswith(":/data")
    assert "montage-work:/work" in volumes
    assert "montage-work" in compose["volumes"]
    assert "env_file" not in svc                                              # секреты хоста монтажу не передаются


def test_hub_allow_covers_compose_network():
    allow = _env(_compose()["services"]["core"])["GITS_HUB_ALLOW"]
    default = allow.split(":-", 1)[1].rstrip("}").split(",")
    nets = [ipaddress.ip_network(n) for n in default]
    assert any(ipaddress.ip_address("172.20.0.3") in n for n in nets)         # типовая подсеть compose


def test_pi_secret_is_not_baked_or_mounted_by_default():
    text = COMPOSE.read_text(encoding="utf-8")
    svc = _compose()["services"]["montage"]
    assert not any("pi_auth" in v or "auth.json" in v for v in svc["volumes"])    # секция закомментирована
    assert "# - ${GITS_PI_AUTH_FILE}:/run/secrets/pi_auth.json:ro" in text
    assert "COPY" not in "".join(ln for ln in DOCKERFILE.read_text(encoding="utf-8").splitlines() if "auth.json" in ln)


def test_dockerfile_has_toolchain_and_non_root_user():
    text = DOCKERFILE.read_text(encoding="utf-8")
    for needle in ("python:3.12-slim", "node:20-slim", "ffmpeg", "npm ci", "npx remotion browser ensure",
                   "services/agent-runtime", "USER gits", "MONTAGE_OUT_DIR=/data/montage/out"):
        assert needle in text, needle


def test_dockerignore_keeps_secrets_and_host_artifacts_out():
    lines = [ln.strip() for ln in IGNORE.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines[0] == "*"                                                     # белый список
    for needed in ("**/.venv", "**/node_modules", "**/.env", "services/agent-runtime/auth.json",
                   "services/agent-runtime/memory", "modules/montage/jobs"):
        assert needed in lines, needed
    assert "!resources" not in lines and "!agent" not in lines                # персонаж и память не попадают в образ


def test_root_gitignore_ignores_montage_out():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "resources/*/montage/out/" in lines
