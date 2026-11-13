"""Фаза 1 переименования: роли workspace / service, ownership, hello с id и role, алиасы окружения, три клиента."""
import pytest
from helpers import MANIFEST, running

from gits_sdk import ModuleClient, PeerClient, ServiceClient, WorkspaceClient
from gits_sdk import protocol as P
from gits_sdk.protocol import Manifest, ManifestError, validate_hello, validate_manifest
from gits_sdk.testing import FakeCore


def man(**extra):
    return {**MANIFEST, **extra}


def service_manifest(**extra):
    return {"id": "pet", "title": "Питомец", "description": "Сервис для проверки.", "version": "1.0.0", "role": "service", **extra}


# ---------------------------------------------------------------- манифест

def test_manifest_without_role_is_workspace():
    assert validate_manifest(MANIFEST) == []
    assert P.role_of(MANIFEST) == "workspace" and P.ownership_of(MANIFEST) == "owned"
    m = Manifest.from_dict(MANIFEST)
    assert m.role is None and m.effective_role == "workspace" and m.effective_ownership == "owned"
    assert "role" not in m.to_dict() and "ownership" not in m.to_dict()        # прежние манифесты не меняются


@pytest.mark.parametrize("role,ownership", [("workspace", "shared"), ("workspace", "owned"), ("workspace", None), (None, "shared")])
def test_workspace_roles_and_ownership_pass(role, ownership):
    extra = {k: v for k, v in (("role", role), ("ownership", ownership)) if v}
    assert validate_manifest(man(**extra)) == []
    m = Manifest.from_dict(man(**extra))
    assert m.effective_ownership == (ownership or "owned")
    assert Manifest.from_dict(m.to_dict()).to_dict() == m.to_dict()


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


def test_workspace_without_workspace_description_is_valid():
    assert validate_manifest(man(role="workspace", ownership="shared")) == []


# ---------------------------------------------------------------- hello

def test_hello_carries_id_role_and_legacy_module_id():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    assert body["id"] == body["module_id"] == "demo" and body["role"] == "workspace"
    assert validate_hello(body) == []
    assert P.hello_body(service_manifest(), "i", 1)["role"] == "service"


def test_old_hello_with_only_module_id_is_accepted():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    old = {k: v for k, v in body.items() if k not in ("id", "role")}
    assert validate_hello(old) == []
    assert P.hello_id(old) == "demo"


def test_new_hello_with_only_id_is_accepted():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    body.pop("module_id")
    assert validate_hello(body) == []


def test_hello_mismatches_are_rejected():
    body = P.hello_body(MANIFEST, "inst0001", 1)
    assert any("не совпадает" in p for p in validate_hello({**body, "module_id": "other"}))
    assert any("manifest.id" in p for p in validate_hello({**body, "id": "other", "module_id": "other"}))
    assert any("role" in p for p in validate_hello({**body, "role": "service"}))
    assert validate_hello({k: v for k, v in body.items() if k not in ("id", "module_id")})


# ---------------------------------------------------------------- окружение

def test_env_aliases_new_names_win(monkeypatch):
    for name in ("GITS_ID", "GITS_TOKEN", "GITS_MODULE_ID", "GITS_MODULE_TOKEN", "GLADOS_MODULE_TOKEN", "GLADOS_MODULE_ID"):
        monkeypatch.delenv(name, raising=False)
    assert P.env_id() is None and P.env_token() is None
    monkeypatch.setenv("GITS_MODULE_ID", "old")
    monkeypatch.setenv("GITS_MODULE_TOKEN", "old-token")
    assert P.env_id() == "old" and P.env_token() == "old-token"                # алиасы читаются, если новых нет
    monkeypatch.setenv("GITS_ID", "new")
    monkeypatch.setenv("GITS_TOKEN", "new-token")
    assert P.env_id() == "new" and P.env_token() == "new-token"                # новые имена первыми


def test_ws_paths_and_default_url():
    assert P.WS_PATH == "/module/ws" and P.WS_PATH_NEW == "/ws"
    assert P.DEFAULT_CORE_URL.endswith(P.WS_PATH)


async def test_token_from_new_env_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("GITS_MODULE_TOKEN", raising=False)
    async with FakeCore() as core:
        monkeypatch.setenv("GITS_TOKEN", core.token)
        client = WorkspaceClient(service_manifest(role="workspace"), url=core.url)
        client.reconnect_delays = (0.05,)
        async with running(client, core):
            assert len(core.hellos) == 1


async def _pong(args):
    return {}


# ---------------------------------------------------------------- клиенты

def test_client_classes_and_roles():
    assert issubclass(WorkspaceClient, PeerClient) and issubclass(ServiceClient, PeerClient) and issubclass(ModuleClient, PeerClient)
    assert "уберёт фаза 4" in ModuleClient.__doc__
    assert WorkspaceClient(MANIFEST).role == "workspace" and WorkspaceClient(MANIFEST).ownership == "owned"
    assert ModuleClient(MANIFEST).role == "workspace"                                    # без role: workspace
    assert ModuleClient(service_manifest()).role == "service"                            # роль берётся из манифеста
    assert ModuleClient(man(ownership="shared")).ownership == "shared"
    c = ServiceClient({k: v for k, v in service_manifest().items() if k != "role"})
    assert c.role == "service" and c.manifest["role"] == "service" and c.ownership is None   # роль подставляется
    assert c.id == c.module_id == "pet"


def test_clients_reject_foreign_role():
    with pytest.raises(ManifestError):
        WorkspaceClient(service_manifest())
    with pytest.raises(ManifestError):
        ServiceClient(man(role="workspace"))
    with pytest.raises(ManifestError):
        ServiceClient(service_manifest(ownership="shared"))


NO_TOOLS = man(tools=[])


@pytest.mark.parametrize("make,manifest,role", [
    (ModuleClient, NO_TOOLS, "workspace"),
    (WorkspaceClient, NO_TOOLS, "workspace"),
    (WorkspaceClient, man(tools=[], role="workspace", ownership="shared"), "workspace"),
    (ServiceClient, service_manifest(), "service"),
    (ModuleClient, service_manifest(), "service"),
])
async def test_clients_connect_to_fake_core(make, manifest, role):
    async with FakeCore() as core:
        client = make(manifest, url=core.url, token=core.token)
        client.reconnect_delays = (0.05,)
        for t in manifest.get("tools") or []:
            client.tool(t["name"])(_pong)
        async with running(client, core):
            hello = core.hellos[0]
            assert hello["id"] == hello["module_id"] == manifest["id"]               # шлёт оба имени
            assert hello["role"] == role
            assert core.conn.module_id == manifest["id"] and core.conn.role == role
            assert not core.violations and not core.rejected


async def test_fake_core_accepts_old_hello_without_id_and_role():
    """Старый клиент (только module_id, без role) подключается к FakeCore как workspace."""
    import aiohttp
    async with FakeCore() as core, aiohttp.ClientSession() as s:
        ws = await s.ws_connect(core.url, headers={"Authorization": f"Bearer {core.token}"})
        body = P.hello_body(MANIFEST, "inst0001", 1)
        del body["id"], body["role"]
        await ws.send_str(P.Envelope("hello", body).dumps())
        reply = P.Envelope.loads((await ws.receive()).data)
        assert reply.type == "welcome"
        assert core.conn.module_id == "demo" and core.conn.role == "workspace"
        await ws.close()
