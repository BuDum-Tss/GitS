"""Контейнер монтажа: ОДИН общий сервис `montage` в корневом docker-compose.yml (не в compose ядра), Dockerfile и .gitignore (разбор файлов, Docker не нужен)."""
import ipaddress
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"                                       # монтаж: общий контейнер, корневой compose
CORE_COMPOSE = ROOT / "docker" / "core" / "compose.yml"
DOCKERFILE = ROOT / "docker" / "montage" / "Dockerfile"
IGNORE = ROOT / "docker" / "montage" / "Dockerfile.dockerignore"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_montage_is_one_shared_container_in_root_compose_and_waits_for_lease():
    compose = _compose()
    svc = compose["services"]["montage"]
    assert "profiles" not in svc and svc["container_name"] == "montage"       # один контейнер на всех персонажей
    assert svc["image"].startswith("${MONTAGE_IMAGE:-gits-montage:dev")
    assert svc["build"]["context"] == "." and "espeech" in compose["services"]
    core = yaml.safe_load(CORE_COMPOSE.read_text(encoding="utf-8"))
    assert set(core["services"]) == {"core"} and set(core["volumes"]) == {"memory"}      # в compose ядра монтажа нет
    assert compose["name"] == core["name"] == "gits"                          # один проект: общая сеть gits_default, имя montage резолвится из ядра
    assert "networks" not in compose and "networks" not in core and "network_mode" not in svc
    peers = yaml.safe_load((ROOT / "agent" / "peers.container.yaml").read_text(encoding="utf-8"))
    assert next(w for w in peers["workspaces"] if w["id"] == "montage")["control_url"] == "http://montage:8780"
    assert "depends_on" not in svc                                             # к ядру при старте не подключается: ждёт аренды
    assert svc["build"]["dockerfile"] == "docker/montage/Dockerfile"


def test_montage_service_environment_and_volumes():
    compose = _compose()
    svc = compose["services"]["montage"]
    env = _env(svc)
    assert env["GITS_CONTROL_TOKEN"].startswith("${GITS_CONTROL_TOKEN") and env["GITS_CONTROL_PORT"] == "8780"
    for gone in ("GITS_CORE_URL", "GITS_TOKEN", "GITS_MODULE_TOKEN", "CHARACTER_HOME", "MONTAGE_OUT_DIR"):
        assert gone not in env, gone                                          # токен шины приходит в acquire
    assert env["GITS_CHARACTERS_ROOT"] == "/characters"
    assert env["MEMORY_DIR"] == "/work" and env["MONTAGE_JOBS_DIR"].startswith("/work/")
    assert "8780" in [str(p) for p in svc["expose"]] and "ports" not in svc   # порт управления не публикуется на хост
    assert "/status" in " ".join(svc["healthcheck"]["test"])
    core_env = _env(yaml.safe_load(CORE_COMPOSE.read_text(encoding="utf-8"))["services"]["core"])
    assert core_env["GITS_CORE_PUBLIC_URL"] == "ws://${CHARACTER_ID:-glados}-core:8766/ws"
    volumes = svc["volumes"]
    assert "./resources/GlaDOS:/characters/glados" in volumes                 # папка каждого персонажа отдельной строкой
    assert "montage_work:/work" in volumes
    assert compose["volumes"]["montage_work"]["name"] == "gits-montage-work"       # общий том, внутри по персонажам
    assert "env_file" not in svc                                              # секреты хоста монтажу не передаются


def test_hub_allow_covers_compose_network():
    allow = _env(yaml.safe_load(CORE_COMPOSE.read_text(encoding="utf-8"))["services"]["core"])["GITS_HUB_ALLOW"]
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
