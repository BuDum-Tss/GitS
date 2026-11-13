"""Режим контейнера (`GITS_CONTAINER=1`): файл участников, допуск шины по сети, токен из окружения, без трея. Docker не нужен."""
from pathlib import Path

import aiohttp
import pytest
import yaml
from hub_helpers import running_core

import supervisor
import tray_launch
from environments.registry import Registry
from gits_sdk import protocol as P
from peer_hub import PeerHub, parse_allow, remote_allowed
from peer_registry import PeerRegistry

ROOT = Path(__file__).resolve().parents[2]
CONTAINER_FILE = ROOT / "agent" / "peers.container.yaml"
HARDWARE = {"voice", "pet", "desktop"}


# ── файл модулей ────────────────────────────────────────────────────────────────

def test_peers_file_default_and_override(tmp_path):
    assert supervisor.peers_file({}) == supervisor.PEERS_YAML
    assert supervisor.peers_file({"GITS_PEERS_FILE": "  "}) == supervisor.PEERS_YAML
    assert supervisor.peers_file({"GITS_PEERS_FILE": "agent/x.yaml"}, tmp_path) == tmp_path / "agent" / "x.yaml"
    absolute = tmp_path / "m.yaml"
    assert supervisor.peers_file({"GITS_PEERS_FILE": str(absolute)}, tmp_path) == absolute
    assert supervisor.peers_file({"GITS_MODULES_FILE": "agent/old.yaml"}, tmp_path) == tmp_path / "agent" / "old.yaml"     # алиас
    assert supervisor.peers_file({"GITS_PEERS_FILE": "agent/new.yaml", "GITS_MODULES_FILE": "agent/old.yaml"},
                                 tmp_path) == tmp_path / "agent" / "new.yaml"                                          # новое имя главнее


def test_load_specs_follows_env_file(tmp_path, monkeypatch):
    other = tmp_path / "other.yaml"
    other.write_text("workspaces:\n  - id: solo\n    cwd: .\n    command: python\n", encoding="utf-8")
    monkeypatch.setenv("GITS_PEERS_FILE", str(other))
    assert [s.id for s in supervisor.load_specs()] == ["solo"]
    monkeypatch.delenv("GITS_PEERS_FILE")
    assert "telegram" in [s.id for s in supervisor.load_specs()]          # без переменной прежний agent/peers.yaml
    assert [s.id for s in supervisor.load_specs(other)] == ["solo"]       # явный путь главнее


def test_container_peers_file_is_valid_and_hardware_free(monkeypatch):
    monkeypatch.setenv("GITS_PEERS_FILE", "agent/peers.container.yaml")
    assert supervisor.peers_file() == CONTAINER_FILE
    specs = {s.id: s for s in supervisor.load_specs()}
    assert set(specs) == {"telegram", "twitch", "obs"}
    main = {s.id: s for s in supervisor.load_specs(ROOT / "agent" / "peers.yaml")}
    for module_id, spec in specs.items():
        assert spec.role == "workspace" and spec.ownership == "owned" and spec.autostart == main[module_id].autostart and spec.restart == main[module_id].restart
        assert spec.cwd == main[module_id].cwd and spec.args == main[module_id].args
    assert not set(specs) & HARDWARE
    assert not any(s.external for s in specs.values())            # decider и espeech внешние
    assert supervisor.load_planned(CONTAINER_FILE) == []


def test_container_peers_file_has_no_hardware_modules_in_text():
    data = yaml.safe_load(CONTAINER_FILE.read_text(encoding="utf-8"))
    ids = {m["id"] for m in data["workspaces"]}
    assert not ids & (HARDWARE | {"decider", "espeech", "rimworld", "montage"})
    for m in data["workspaces"]:
        for hw in HARDWARE:
            assert f"modules/{hw}" not in m["cwd"]


# ── допуск шины ─────────────────────────────────────────────────────────────────

def test_parse_allow_skips_garbage():
    nets = parse_allow("172.16.0.0/12, 192.168.0.0/16,мусор,,10.1.2.3")
    assert [str(n) for n in nets] == ["172.16.0.0/12", "192.168.0.0/16", "10.1.2.3/32"]
    assert parse_allow("") == [] and parse_allow(None) == []


def test_remote_allowed_default_is_loopback_only():
    for ok in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert remote_allowed(ok)
    for bad in ("172.18.0.1", "192.168.1.5", "10.0.0.1", "", None, "не адрес"):
        assert not remote_allowed(bad)


def test_remote_allowed_with_networks():
    nets = parse_allow("172.16.0.0/12,192.168.0.0/16")
    assert remote_allowed("172.18.0.1", nets) and remote_allowed("192.168.65.1", nets)
    assert remote_allowed("::ffff:172.18.0.1", nets)
    assert remote_allowed("127.0.0.1", nets)
    assert not remote_allowed("8.8.8.8", nets) and not remote_allowed("10.0.0.1", nets) and not remote_allowed("::2", nets)


async def test_hub_lets_allowed_network_in_and_refuses_stranger(tmp_path):
    nets = parse_allow("172.16.0.0/12")
    async with running_core(tmp_path, spoof_remote="172.18.0.1", allow=nets) as core:
        async with aiohttp.ClientSession() as s:
            ws = await s.ws_connect(core.url, headers={"Authorization": f"Bearer {core.token}"})      # из сети: рукопожатие идёт
            await ws.close()
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(core.url, headers={"Authorization": "Bearer не тот"})              # токен остаётся обязательным
            assert e.value.status == 401
    async with running_core(tmp_path, spoof_remote="203.0.113.7", allow=nets) as core:
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(core.url, headers={"Authorization": f"Bearer {core.token}"})
            assert e.value.status == 403


def test_hub_reads_allow_from_environment():
    hub = PeerHub(PeerRegistry(Registry()), environ={P.ENV_HUB_ALLOW: "192.168.0.0/16"})
    assert [str(n) for n in hub.allow] == ["192.168.0.0/16"]
    assert PeerHub(PeerRegistry(Registry()), environ={}).allow == []        # по умолчанию только петля


# ── токен ───────────────────────────────────────────────────────────────────────

async def test_container_token_from_environment_and_no_file(tmp_path, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "_dir", tmp_path)
    hub = PeerHub(PeerRegistry(Registry()), environ={"GITS_CONTAINER": "1", P.ENV_TOKEN: "test-token"})
    assert hub.token == "test-token"
    await hub.start()
    await hub.stop()
    assert not (tmp_path / "module_token").exists()


async def test_container_token_legacy_alias_and_new_name_priority(tmp_path, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "_dir", tmp_path)
    legacy = PeerHub(PeerRegistry(Registry()), environ={"GITS_CONTAINER": "1", P.ENV_MODULE_TOKEN: "old-token"})
    assert legacy.token == "old-token"                                         # прежнее имя GITS_MODULE_TOKEN читается как алиас
    both = PeerHub(PeerRegistry(Registry()), environ={"GITS_CONTAINER": "1", P.ENV_MODULE_TOKEN: "old-token",
                                                      P.ENV_TOKEN: "new-token"})
    assert both.token == "new-token"                                           # GITS_TOKEN главнее


async def test_container_without_token_writes_file_to_memory_dir(tmp_path, monkeypatch):
    import settings
    monkeypatch.setattr(settings, "_dir", tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    hub = PeerHub(PeerRegistry(Registry()), environ={"GITS_CONTAINER": "1"})
    await hub.start()
    await hub.stop()
    assert (tmp_path / "module_token").read_text(encoding="utf-8") == hub.token and len(hub.token) == 64
    assert not (tmp_path / "local").exists()                                    # не в LOCALAPPDATA


def test_token_from_environment_is_ignored_outside_container():
    hub = PeerHub(PeerRegistry(Registry()), environ={P.ENV_TOKEN: "из .env хоста"})
    assert hub.token != "из .env хоста" and len(hub.token) == 64               # хост: токен создаётся при каждом старте, как раньше


# ── трей ────────────────────────────────────────────────────────────────────────

def test_tray_does_not_start_in_container(tmp_path):
    (tmp_path / "tray.py").write_text("")
    calls = []
    assert not tray_launch.wanted({"GITS_CONTAINER": "1"}, "win32")
    assert tray_launch.wanted({"GITS_CONTAINER": "0"}, "win32")
    assert not tray_launch.ensure_tray(env={"GITS_CONTAINER": "1"}, platform="win32", here=tmp_path,
                                       spawn=lambda *a, **k: calls.append(a))
    assert calls == []
