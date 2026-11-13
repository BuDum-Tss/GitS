"""Вкладки «Окружения» и «Сервисы» дашборда: записи делятся по `role` (workspace, service) и `kind: planned`, у каждой есть title
из peers.yaml, пример для разработчиков (echo) виден только при настройке dev_mode, запланированные супервизор не запускает."""
import json
from pathlib import Path

from aiohttp.test_utils import make_mocked_request
from hub_helpers import FakeCompanion

import core_service
import dashboard
import settings
import speech_out
from environments.registry import Registry
from supervisor import load_planned, load_specs

AGENT = Path(__file__).resolve().parent.parent


class WiredCompanion(FakeCompanion):
    def __init__(self):
        super().__init__()
        self.presence = speech_out.PresenceTracker()
        self.io = None
        self.profile_hint = lambda: ""


def wire():
    """Ядро с настоящим peers.yaml, но без запуска: процессы не нужны, проверяется то, что видит дашборд."""
    layer = core_service.wire_modules(WiredCompanion(), Registry(), port=1, battery=lambda: {"level": 1.0, "sleeping": False})
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    return layer


async def rows():
    resp = await dashboard.handle_api_modules(make_mocked_request("GET", "/api/modules"))
    return json.loads(resp.text)


async def test_peers_are_grouped_by_role_and_carry_titles():
    wire()
    by_id = {r["id"]: r for r in await rows()}
    roles = {r["id"]: r.get("role") for r in by_id.values()}
    assert roles["telegram"] == roles["rimworld"] == roles["desktop"] == roles["montage"] == "workspace"
    assert roles["voice"] == roles["pet"] == roles["decider"] == roles["espeech"] == "service"
    assert by_id["minecraft"]["kind"] == "planned"
    assert [by_id[i]["ownership"] for i in ("telegram", "streamer")] == ["owned"] * 2
    assert [by_id[i]["ownership"] for i in ("desktop", "rimworld", "montage")] == ["shared"] * 3
    assert by_id["voice"]["ownership"] is None and by_id["decider"]["external"] and not by_id["voice"]["external"]
    for row in by_id.values():
        assert row["title"] and (row.get("kind") == "planned" or row["role"] in ("workspace", "service"))
    decider = by_id["decider"]
    assert decider["title"] == "Классификатор внимания"
    assert decider["description"] == "часть ядра: быстрая оценка событий; работает отдельным процессом (Python 3.12 + torch)"
    assert by_id["voice"]["title"] == "Голос"


async def test_planned_modules_come_last_with_title_and_tz():
    wire()
    result = await rows()
    kinds = [r.get("kind") for r in result]
    assert kinds[-1:] == ["planned"] and "planned" not in kinds[:-1]                      # серые карточки в конце списка
    planned = {r["id"]: r for r in result if r.get("kind") == "planned"}
    assert planned["minecraft"]["title"] == "Minecraft" and planned["minecraft"]["tz"] == ""
    assert "montage" not in planned and "desktop" not in planned


async def test_echo_is_hidden_without_dev_mode_and_shown_with_it():
    wire()
    assert settings.load()["dev_mode"] is False
    assert "echo" not in {r["id"] for r in await rows()}
    settings.update(dev_mode=True)
    echo = next(r for r in await rows() if r["id"] == "echo")
    assert echo["role"] == "workspace" and echo["title"] == "Пример модуля (для разработчиков)"
    assert echo["description"] == "повторяет текст; проверка протокола модулей"
    settings.update(dev_mode=False)
    assert "echo" not in {r["id"] for r in await rows()}


def test_planned_modules_are_not_run_by_the_supervisor():
    layer = wire()
    planned = {p.id for p in load_planned()}
    assert planned == {"minecraft"}
    assert not planned & {s.id for s in load_specs()}                                    # в peers они не попадают
    assert not planned & set(layer.supervisor.specs)
    assert not planned & set(layer.modules.records)
    assert all(layer.supervisor.status(module_id) is None for module_id in planned)


def test_dev_mode_is_a_setting_with_a_form_field():
    assert settings.DEFAULTS["dev_mode"] is False
    html = (AGENT / "static" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "key: 'dev_mode'" in html and 'data-tab="workspaces"' in html and 'data-tab="services"' in html
    assert 'data-tab="modules"' not in html


def test_load_planned_skips_broken_entries(tmp_path):
    path = tmp_path / "peers.yaml"
    path.write_text("workspaces: []\nplanned:\n  - {id: a, title: А, tz: ТЗ 1}\n  - {id: b}\n  - just text\n  - {id: c, title: С}\n",
                    encoding="utf-8")
    assert [(p.id, p.title, p.tz) for p in load_planned(path)] == [("a", "А", "ТЗ 1"), ("c", "С", "")]


async def _by_role(role):
    handler = dashboard.handle_api_workspaces if role == "workspace" else dashboard.handle_api_services
    resp = await handler(make_mocked_request("GET", f"/api/{role}s"))
    return json.loads(resp.text)


async def test_workspaces_and_services_endpoints_split_by_role():
    wire()
    settings.update(dev_mode=True)
    workspaces, services = await _by_role("workspace"), await _by_role("service")
    ws_real = {r["id"] for r in workspaces if r.get("kind") != "planned"}
    assert {"telegram", "streamer", "desktop", "rimworld", "montage", "echo"} == ws_real
    assert [r["id"] for r in workspaces if r.get("kind") == "planned"] == ["minecraft"]      # серые карточки идут к окружениям
    assert {r["id"] for r in services} == {"voice", "pet", "decider", "espeech"}
    assert all(r["role"] == "workspace" for r in workspaces if r.get("kind") != "planned")
    assert all(r["role"] == "service" for r in services)
    everything = {r["id"] for r in await rows() if r.get("kind") != "planned"}                # /api/modules остался алиасом общего списка
    assert everything == ws_real | {r["id"] for r in services}


def test_routes_register_new_and_legacy_paths():
    from dashboard import routes_peers
    paths = {(m, p) for m, p, _ in routes_peers.ROUTES}
    assert {("GET", "/api/modules"), ("GET", "/api/workspaces"), ("GET", "/api/services")} <= paths
    assert ("POST", "/api/modules/{id}/enabled") in paths
