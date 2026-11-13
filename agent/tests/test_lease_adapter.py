"""Аренда общих пространств в ядре: LeaseAdapter на настоящем ControlServer (петля), WorkspaceLink, workspace.switch/status,
дашборд. Docker не нужен; у аренды нет срока и продления."""
import asyncio
import json

import pytest
from aiohttp.test_utils import make_mocked_request
from gits_sdk.control import ControlServer

import dashboard
import settings
from app.lease_adapter import LeaseAdapter
from app.workspace_link import WorkspaceLink
from app.workspace_manager import WorkspaceManager
from domain import workspace as dom
from environments.builtin import _workspace_tools
from supervisor import PeerSpec, Supervisor, load_specs

TOKEN = "control-secret"
CORE_TOKEN = "core-secret-token"
MANIFEST = {"id": "montage", "title": "Монтаж видео", "description": "d", "version": "1.0.0", "tools": [
    {"name": "start", "description": "смонтировать"}],
    "workspace": {"id": "montage", "title": "Монтаж", "purpose": "делать ролики", "when_to_use": "просят смонтировать видео",
                  "keywords": ["монтаж"], "entry": ["user_message", "self_impulse"]}}


class Module:
    """Соединение сервиса пространства с шиной ядра: журнал запросов."""

    def __init__(self):
        self.names = []

    async def request(self, name, body, timeout=0):
        self.names.append(name)
        return {"ok": True, "checkpoint": {"s": 1}} if name == "workspace.exit" else {"ok": True}


class Service:
    """Сервис пространства: настоящий ControlServer; «подключение» к шине это запись в conns при acquire."""

    def __init__(self, conns: dict, mod: Module):
        self.conns, self.mod = conns, mod
        self.server = ControlServer(id="montage", token=TOKEN, ownership="shared", manifest=MANIFEST,
                                    on_acquire=self.on_acquire, on_release=self.on_release, jobs=lambda: self.jobs)
        self.acquired = []
        self.jobs = 0                                                   # активные задачи сервиса

    async def on_acquire(self, lease):
        self.acquired.append(lease)
        self.conns["montage"] = self.mod

    async def on_release(self, lease):
        if lease.why in ("release", "finished") and self.jobs:
            return False                                                # доделывает задачи
        self.conns.pop("montage", None)


@pytest.fixture
async def space():
    conns, mod = {}, Module()
    svc = Service(conns, mod)
    await svc.server.start()
    yield svc
    await svc.server.stop()


def adapter_for(svc, character="glados"):
    ad = LeaseAdapter(character=lambda: character, core_url=lambda: "ws://glados-core:8766/ws", core_token=lambda: CORE_TOKEN)
    ad.register("montage", svc.server.url, TOKEN)
    return ad


async def test_acquire_busy_release_without_renewal(space):
    me, other = adapter_for(space, "glados"), adapter_for(space, "other")
    await me.refresh()
    assert me.view("montage")["state"] == "free"
    got = await me.acquire("montage", "смонтировать шортс")
    assert got["ok"] and got["lease_id"] and me.holds("montage")
    assert space.acquired[0].core_url == "ws://glados-core:8766/ws" and space.acquired[0].token == CORE_TOKEN
    assert not hasattr(me, "renew")                                    # продления нет
    busy = await other.acquire("montage", "своё")
    assert busy["busy"] and busy["holder"]["character"] == "glados" and busy["holder"]["purpose"] == "смонтировать шортс"
    assert "core_url" not in busy["holder"] and CORE_TOKEN not in json.dumps(busy)
    await other.refresh()
    assert other.view("montage")["state"] == "busy"
    await me.refresh()
    assert me.view("montage")["state"] == "mine"
    assert (await me.release("montage"))["released"] is True and not me.holds("montage")
    assert (await other.acquire("montage", "своё"))["ok"]


async def test_catalog_comes_from_service_info_before_lease(space):
    ad = adapter_for(space)
    assert ad.info("montage") is None                                   # до первого опроса ничего нет
    await ad.refresh()
    row = ad.catalog()[0]
    assert row["title"] == "Монтаж" and row["purpose"] == "делать ролики" and row["when_to_use"] == "просят смонтировать видео"
    assert row["lease"]["state"] == "free"
    ws = ad.manifest_of("montage")["workspace"]
    assert ws["when_to_use"] == "просят смонтировать видео" and ws["entry"] == ["user_message", "self_impulse"]


async def test_unreachable_service_is_offline_and_missing_token_disables_lease():
    ad = LeaseAdapter(character=lambda: "glados", core_url=lambda: "ws://x/ws", core_token=lambda: "t")
    ad.register("montage", "http://127.0.0.1:9", TOKEN)
    await ad.refresh()
    assert ad.view("montage")["state"] == "offline" and ad.view("montage")["reason"]
    got = await ad.acquire("montage", "x")
    assert not got["ok"] and not got.get("busy") and got["reason"]
    ad.register("montage", "http://127.0.0.1:9", "")
    got = await ad.acquire("montage", "x")
    assert not got["ok"] and "GITS_CONTROL_TOKEN" in got["reason"]
    assert ad.view("montage")["state"] == "offline"


def link_for(ad, conns):
    return WorkspaceLink(connection_of=conns.get, leases=ad, connect_wait_s=1.0, poll_s=0.01)


async def test_activate_takes_lease_waits_for_connection_then_enters(space):
    ad, conns = adapter_for(space), space.conns
    res = await link_for(ad, conns).activate("montage", "ролик")
    assert res["ok"] and space.mod.names == ["workspace.enter"] and ad.holds("montage")
    await ad.refresh()
    assert ad.view("montage")["state"] == "mine"


async def test_activate_waits_for_late_connection(space):
    conns = space.conns

    async def late(lease):
        space.acquired.append(lease)

        async def connect_later():
            await asyncio.sleep(0.1)
            conns["montage"] = space.mod
        asyncio.get_running_loop().create_task(connect_later())
    space.server._on_acquire = late
    res = await link_for(adapter_for(space), conns).activate("montage", "ролик")
    assert res["ok"] and space.mod.names == ["workspace.enter"]


async def test_activate_busy_refuses_without_enter(space):
    conns = space.conns
    await adapter_for(space, "other").acquire("montage", "чужая цель")
    conns.pop("montage")
    ad = adapter_for(space, "glados")
    res = await link_for(ad, conns).activate("montage", "моя цель")
    assert not res["ok"] and res["busy"] and res["holder"]["character"] == "other"
    assert "other" in res["reason"] and "чужая цель" in res["reason"] and "позже" in res["reason"]
    assert space.mod.names == []


async def test_activate_fails_and_releases_when_service_never_connects(space):
    async def silent(lease):
        space.acquired.append(lease)                                    # подключения к шине нет
    space.server._on_acquire = silent
    ad = adapter_for(space)
    link = WorkspaceLink(connection_of=lambda m: None, leases=ad, connect_wait_s=0.05, poll_s=0.01)
    res = await link.activate("montage", "x")
    assert not res["ok"] and "не подключилось" in res["reason"]
    assert space.server.lease is None and not ad.holds("montage")


async def test_release_sends_exit_then_frees_lease_even_after_forced_refusal(space):
    ad, conns = adapter_for(space), space.conns
    link = link_for(ad, conns)
    await link.activate("montage", "ролик")
    assert (await link.release("user"))["ok"]
    assert space.mod.names == ["workspace.enter", "workspace.exit"]
    assert space.server.lease is None and not ad.holds("montage")
    assert link.checkpoints["montage"]["payload"] == {"s": 1}
    link = link_for(ad, conns)                                          # отказ без force оставляет аренду, с force отпускает
    await link.activate("montage", "ещё")

    async def refuse(name, body, timeout=0):
        space.mod.names.append(name)
        return {"ok": False, "error": "небезопасная точка"} if name == "workspace.exit" else {"ok": True}
    space.mod.request = refuse
    assert not (await link.release("self"))["ok"] and space.server.lease is not None
    assert (await link.release("disconnect", force=True))["ok"] and space.server.lease is None


async def test_forget_releases_lease(space):
    ad, conns = adapter_for(space), space.conns
    link = link_for(ad, conns)
    await link.activate("montage", "ролик")
    link.forget("montage")                                              # участник отключился
    for _ in range(100):
        if space.server.lease is None:
            break
        await asyncio.sleep(0.01)
    assert space.server.lease is None and link.holder is None


async def test_unleased_modules_work_as_before():
    mod = Module()
    ad = LeaseAdapter(character=lambda: "g", core_url=lambda: "ws://x/ws", core_token=lambda: "t")     # rimworld без аренды
    link = WorkspaceLink(connection_of=lambda m: mod if m == "rimworld" else None, leases=ad)
    assert (await link.activate("rimworld", ""))["ok"] and (await link.release("user"))["ok"]
    assert mod.names == ["workspace.enter", "workspace.exit"]


def manager_for(ad, conns):
    link = link_for(ad, conns)
    mgr = WorkspaceManager(
        activate=link.activate, release=lambda ws, reason, force: link.release(reason=reason, force=force),
        connected=lambda ws: ws in conns, leased=ad.manages, lease_view=ad.view,
        modules_of=lambda: [("montage", ad.manifest_of("montage"), "Монтаж")] if ad.manifest_of("montage") else [])
    mgr.refresh_leases = ad.refresh
    return mgr


async def test_switch_describes_space_and_reports_busy_in_plain_words(space):
    conns = space.conns
    await adapter_for(space, "other").acquire("montage", "чужая цель")
    conns.pop("montage")
    ad = adapter_for(space, "glados")
    await ad.refresh()
    mgr = manager_for(ad, conns)
    tools = {t.name: t for t in _workspace_tools(mgr)}
    status = await tools["status"].call({})
    assert "Монтаж" in status and "делать ролики" in status and "просят смонтировать видео" in status
    assert "занято персонажем other" in status and "чужая цель" in status
    with pytest.raises(Exception) as e:
        await tools["switch"].call({"workspace": "montage", "goal": "ролик"})
    assert "занято" in str(e.value) and "other" in str(e.value) and "чужая цель" in str(e.value)
    assert mgr.current == dom.ASSISTANT


async def test_switch_into_free_space_takes_lease_without_prior_connection(space):
    ad = adapter_for(space)
    await ad.refresh()                                                  # ядро читает описание при старте
    mgr = manager_for(ad, space.conns)
    res = await mgr.enter("montage", dom.Reason.USER, "ролик")           # сервис не подключён: аренда подключит
    assert res["ok"] and mgr.current == "montage" and space.server.lease.character == "glados"
    res = await mgr.leave(dom.Reason.USER)
    assert res["ok"] and space.server.lease is None


async def test_peer_snapshot_and_api_show_lease_and_space_without_secrets(space, monkeypatch):
    import core_service
    import speech_out
    from environments.registry import Registry
    from hub_helpers import FakeCompanion

    class Companion(FakeCompanion):
        def __init__(self):
            super().__init__()
            self.presence = speech_out.PresenceTracker()
            self.io = None
            self.profile_hint = lambda: ""
    monkeypatch.setenv("GITS_CONTROL_TOKEN", TOKEN)
    spec = PeerSpec(id="montage", cwd=".", command="", ownership="shared", unmanaged=True, control_url=space.server.url,
                    title="Монтаж видео")
    layer = core_service.wire_modules(Companion(), Registry(), port=1, specs=[spec], battery=lambda: {"level": 1.0, "sleeping": False})
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    settings.update(dev_mode=False)

    async def row():
        resp = await dashboard.handle_api_workspaces(make_mocked_request("GET", "/api/workspaces"))
        return next(r for r in json.loads(resp.text) if r["id"] == "montage"), resp.text
    r, _ = await row()
    assert r["lease"]["state"] == "free" and r["lease"]["holder"] is None
    assert r["space"]["purpose"] == "делать ролики" and r["space"]["when_to_use"] == "просят смонтировать видео"
    assert r["status"] == "manual"                                      # процесс запускает compose, не супервизор
    note = "\n".join(layer.modules.notes())
    assert "Монтаж" in note and "просят смонтировать видео" in note     # агент видит пространство до подключения к шине
    space.conns["montage"] = space.mod                                  # подключение сервиса к шине (в тесте условное)
    layer.link.connection_of = space.conns.get
    assert (await layer.link.activate("montage", "цель"))["ok"]
    r, text = await row()
    assert r["lease"]["state"] == "mine" and r["lease"]["holder"]["character"]
    assert TOKEN not in text and layer.hub.token not in text and "core_url" not in text
    await layer.link.release("user")


def test_load_specs_reads_external_shared_entry(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("workspaces:\n  - id: montage\n    ownership: shared\n    external: true\n    control_url: http://montage:8780\n"
                 "  - id: bad\n    ownership: owned\n    external: true\n  - id: nocmd\n    ownership: shared\n", encoding="utf-8")
    specs = {s.id: s for s in load_specs(f)}
    assert set(specs) == {"montage", "nocmd"} - {"nocmd"}              # нет command у управляемого и external у owned: запись пропущена
    m = specs["montage"]
    assert m.unmanaged and m.control_url == "http://montage:8780" and m.control_token_env == "GITS_CONTROL_TOKEN"
    sup = Supervisor([m], token="t", core_url="ws://x/ws")
    assert not sup.should_run("montage") and sup.status("montage")["status"] == "manual"


async def test_exit_with_active_jobs_keeps_lease_mine_then_finishes(space):
    ad, conns = adapter_for(space), space.conns
    other = adapter_for(space, "other")
    link = link_for(ad, conns)
    await link.activate("montage", "ролик")
    space.jobs = 2
    res = await link.release("user")                                    # выход не ждёт задач и не считает это ошибкой
    assert res["ok"] and link.holder is None and "montage" in conns
    assert space.server.lease is not None and space.server.lease.releasing
    assert ad.holds("montage") and (await ad.release("montage"))["pending"] == "jobs"
    await ad.refresh()
    view = ad.view("montage")
    assert view["state"] == "mine" and view["releasing"] and view["jobs"] == 2
    await other.refresh()
    assert other.view("montage")["state"] == "busy"
    busy = await other.acquire("montage", "чужое")
    assert busy["busy"] and busy["holder"]["releasing"] and "доделывает" in other.busy_reason("montage", busy["holder"])
    space.jobs = 0                                                      # задача кончилась: сервис освобождает сам
    assert await space.server.finish_release(space.server.lease.lease_id)
    await ad.refresh()
    assert ad.view("montage")["state"] == "free" and not ad.holds("montage") and "montage" not in conns


async def test_switch_back_into_releasing_space_and_status_text(space):
    ad, conns = adapter_for(space), space.conns
    await ad.refresh()
    mgr = manager_for(ad, conns)
    assert (await mgr.enter("montage", dom.Reason.USER, "ролик"))["ok"]
    space.jobs = 1
    assert (await mgr.leave(dom.Reason.USER))["ok"]
    tools = {t.name: t for t in _workspace_tools(mgr)}
    assert "моё, дорабатывает задачи (1)" in await tools["status"].call({})
    lease_id = space.server.lease.lease_id
    res = await mgr.enter("montage", dom.Reason.USER, "ролик")           # тот же персонаж возвращается
    assert res["ok"] and mgr.current == "montage"
    assert space.server.lease.lease_id == lease_id and not space.server.lease.releasing
    assert space.mod.names.count("workspace.enter") == 2


async def test_dashboard_shows_releasing_badge_data(space):
    ad = adapter_for(space)
    await ad.refresh()
    link = link_for(ad, space.conns)
    await link.activate("montage", "ролик")
    space.jobs = 3
    await link.release("user")
    await ad.refresh()
    v = ad.view("montage")
    assert (v["state"], v["releasing"], v["jobs"]) == ("mine", True, 3)
