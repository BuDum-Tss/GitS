"""Роли workspace / service, ownership, hello с id и role, три клиента; контракт 2.0: role и ownership обязательны, алиасов нет."""
import aiohttp
import pytest
from helpers import MANIFEST, running

from gits_sdk import PeerClient, ServiceClient, WorkspaceClient
from gits_sdk import protocol as P
from gits_sdk.protocol import Manifest, ManifestError, validate_hello, validate_manifest
from gits_sdk.testing import FakeCore


def man(**extra):
    return {**MANIFEST, **extra}


def without(manifest: dict, *keys: str) -> dict:
    return {k: v for k, v in manifest.items() if k not in keys}


def service_manifest(**extra):
    return {"id": "pet", "title": "Питомец", "description": "Сервис для проверки.", "version": "1.0.0", "role": "service", **extra}


# ---------------------------------------------------------------- манифест

def test_manifest_requires_role():
    assert validate_manifest(MANIFEST) == []
    problems = validate_manifest(without(MANIFEST, "role", "ownership"))
    assert any(p.startswith("role:") and "обязательное" in p for p in problems)
    with pytest.raises(ManifestError):
        Manifest.from_dict(without(MANIFEST, "role", "ownership"))
    with pytest.raises(ManifestError):
        WorkspaceClient(without(MANIFEST, "role"))
    with pytest.raises(ManifestError):
        ServiceClient(without(service_manifest(), "role"))


def test_workspace_requires_ownership():
    problems = validate_manifest(without(MANIFEST, "ownership"))
    assert any(p.startswith("ownership:") and "обязательное" in p for p in problems)
    with pytest.raises(ManifestError):
        WorkspaceClient(without(MANIFEST, "ownership"))


@pytest.mark.parametrize("ownership", ["shared", "owned"])
def test_workspace_roles_and_ownership_pass(ownership):
    manifest = man(role="workspace", ownership=ownership)
    assert validate_manifest(manifest) == []
    m = Manifest.from_dict(manifest)
    assert m.role == "workspace" and m.effective_role == "workspace" and m.effective_ownership == ownership
    assert P.role_of(manifest) == "workspace" and P.ownership_of(manifest) == ownership
    assert Manifest.from_dict(m.to_dict()).to_dict() == m.to_dict() and m.to_dict()["role"] == "workspace"


def test_service_manifest_is_valid_and_has_no_ownership():
    assert validate_manifest(service_manifest()) == []
    assert P.ownership_of(service_manifest()) is None
    assert Manifest.from_dict(service_manifest()).effective_ownership is None
    assert validate_manifest(service_manifest(tools=MANIFEST["tools"])) == []          # инструменты у service допустимы


def test_bad_role_and_ownership_are_rejected():
    assert any(p.startswith("role:") for p in validate_manifest(man(role="module")))
    assert any(p.startswith("role:") for p in validate_manifest(man(role=5)))
    assert any(p.startswith("ownership:") for p in validate_manifest(man(ownership="rented")))
    assert any(p.startswith("ownership:") for p in validate_manifest(man(ownership=True)))


def test_service_with_ownership_or_workspace_is_rejected():
    assert any("ownership" in p for p in validate_manifest(service_manifest(ownership="shared")))
    ws = {"id": "pet", "title": "Среда", "entry": ["user_message"]}
    assert any(p.startswith("workspace:") for p in validate_manifest(service_manifest(workspace=ws)))
    ok = man(workspace={"id": "demo", "title": "Среда", "entry": ["user_message"]}, ownership="shared")
    assert validate_manifest(ok) == []                                                   # у workspace описание среды допустимо


# ---------------------------------------------------------------- hello

def test_hello_carries_only_id_and_role():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    assert body["id"] == "demo" and body["role"] == "workspace" and "module_id" not in body and body["protocol"] == 2
    assert validate_hello(body) == []
    assert P.hello_body(service_manifest(), "i", 1)["role"] == "service"


def test_hello_without_role_is_rejected():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    del body["role"]
    assert any(p.startswith("role:") for p in validate_hello(body))


def test_hello_with_module_id_is_rejected():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    assert any("module_id" in p for p in validate_hello({**body, "module_id": "demo"}))
    old = {k: v for k, v in body.items() if k not in ("id", "role")}
    old["module_id"] = "demo"
    assert validate_hello(old)                                                           # старый hello целиком не проходит


def test_hello_mismatches_are_rejected():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    assert any("manifest.id" in p for p in validate_hello({**body, "id": "other"}))
    assert any("role" in p for p in validate_hello({**body, "role": "service"}))
    assert validate_hello({k: v for k, v in body.items() if k != "id"})
    assert any("protocol" in p for p in validate_hello({**body, "protocol": 1}))        # протокол 1 больше не принимается


# ---------------------------------------------------------------- окружение и адрес

def test_env_names(monkeypatch):
    for name in ("GITS_ID", "GITS_TOKEN", "GITS_MODULE_ID", "GITS_MODULE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert P.env_id() is None and P.env_token() is None
    monkeypatch.setenv("GITS_MODULE_ID", "old")
    monkeypatch.setenv("GITS_MODULE_TOKEN", "old-token")
    assert P.env_id() is None and P.env_token() is None                                  # старые имена не читаются
    monkeypatch.setenv("GITS_ID", "new")
    monkeypatch.setenv("GITS_TOKEN", "new-token")
    assert P.env_id() == "new" and P.env_token() == "new-token"


def test_ws_path_and_default_url():
    assert P.WS_PATH == "/ws" and not hasattr(P, "WS_PATH_NEW")
    assert P.DEFAULT_CORE_URL.endswith("/ws") and "module" not in P.DEFAULT_CORE_URL
    assert P.PROTOCOL == 2


async def test_token_from_env_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    async with FakeCore() as core:
        monkeypatch.setenv("GITS_TOKEN", core.token)
        client = WorkspaceClient(man(tools=[]), url=core.url)
        client.reconnect_delays = (0.05,)
        async with running(client, core):
            assert len(core.hellos) == 1


async def _pong(args):
    return {}


# ---------------------------------------------------------------- клиенты

def test_client_classes_and_roles():
    import gits_sdk
    assert issubclass(WorkspaceClient, PeerClient) and issubclass(ServiceClient, PeerClient)
    assert not hasattr(gits_sdk, "ModuleClient") and "ModuleClient" not in gits_sdk.__all__
    assert WorkspaceClient(MANIFEST).role == "workspace" and WorkspaceClient(MANIFEST).ownership == "owned"
    assert WorkspaceClient(man(ownership="shared")).ownership == "shared"
    c = ServiceClient(service_manifest())
    assert c.role == "service" and c.ownership is None and c.id == "pet" and not hasattr(c, "module_id")


def test_clients_reject_foreign_role():
    with pytest.raises(ManifestError):
        WorkspaceClient(service_manifest())
    with pytest.raises(ManifestError):
        ServiceClient(man(role="workspace"))
    with pytest.raises(ManifestError):
        ServiceClient(service_manifest(ownership="shared"))


NO_TOOLS = man(tools=[])


@pytest.mark.parametrize("make,manifest,role", [
    (WorkspaceClient, NO_TOOLS, "workspace"),
    (WorkspaceClient, man(tools=[], role="workspace", ownership="shared"), "workspace"),
    (ServiceClient, service_manifest(), "service"),
])
async def test_clients_connect_to_fake_core(make, manifest, role):
    async with FakeCore() as core:
        client = make(manifest, url=core.url, token=core.token)
        client.reconnect_delays = (0.05,)
        for t in manifest.get("tools") or []:
            client.tool(t["name"])(_pong)
        async with running(client, core):
            hello = core.hellos[0]
            assert hello["id"] == manifest["id"] and "module_id" not in hello
            assert hello["role"] == role
            assert core.conn.id == manifest["id"] and core.conn.role == role
            assert not core.violations and not core.rejected


async def _raw_hello(core, mutate):
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(core.url, headers={"Authorization": f"Bearer {core.token}"})
        body = P.hello_body(MANIFEST, "inst0001", 1)
        mutate(body)
        await ws.send_str(P.Envelope("hello", body).dumps())
        reply = P.Envelope.loads((await ws.receive()).data)
        await ws.close()
        return reply


async def test_fake_core_accepts_only_id_and_role():
    async with FakeCore() as core:
        assert (await _raw_hello(core, lambda b: None)).type == "welcome"


async def test_fake_core_rejects_old_and_incomplete_hello():
    async with FakeCore() as core:
        assert (await _raw_hello(core, lambda b: b.pop("role"))).type == "reject"
        assert (await _raw_hello(core, lambda b: b.update(module_id="demo"))).type == "reject"
        assert (await _raw_hello(core, lambda b: (b.pop("id"), b.pop("role"), b.update(module_id="demo")))).type == "reject"
        assert len(core.rejected) == 3 and not core.hellos
