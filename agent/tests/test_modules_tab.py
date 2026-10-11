"""Вкладка «Модули» дашборда: записи делятся по `kind` (module, service, planned), у каждой есть title из modules.yaml,
пример для разработчиков (echo) виден только при настройке dev_mode, запланированные модули супервизор не запускает."""
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
    """Ядро с настоящим modules.yaml, но без запуска: процессы не нужны, проверяется то, что видит дашборд."""
    layer = core_service.wire_modules(WiredCompanion(), Registry(), port=1, battery=lambda: {"level": 1.0, "sleeping": False})
    dashboard.set_modules(layer.modules, layer.hub, layer.supervisor)
    return layer


async def rows():
    resp = await dashboard.handle_api_modules(make_mocked_request("GET", "/api/modules"))
    return json.loads(resp.text)


async def test_modules_are_grouped_by_kind_and_carry_titles():
    wire()
    by_id = {r["id"]: r for r in await rows()}
    kinds = {r["id"]: r["kind"] for r in by_id.values()}
    assert kinds["voice"] == kinds["pet"] == kinds["telegram"] == kinds["rimworld"] == "module"
    assert kinds["decider"] == "service"
    assert kinds["montage"] == kinds["desktop"] == kinds["minecraft"] == "planned"
    for row in by_id.values():
        assert row["title"] and row["kind"] in ("module", "service", "planned")
    decider = by_id["decider"]
    assert decider["title"] == "Классификатор внимания"
    assert decider["description"] == "часть ядра: быстрая оценка событий; работает отдельным процессом (Python 3.12 + torch)"
    assert by_id["voice"]["title"] == "Голос"


async def test_planned_modules_come_last_with_title_and_tz():
    wire()
    result = await rows()
    kinds = [r["kind"] for r in result]
    assert kinds[-3:] == ["planned"] * 3 and "planned" not in kinds[:-3]                  # серые карточки в конце списка
    planned = {r["id"]: r for r in result if r["kind"] == "planned"}
    assert planned["montage"]["title"] == "Монтаж видео" and planned["montage"]["tz"] == "ТЗ 20–22"
    assert planned["desktop"]["title"] == "Компьютер: экран, мышь, клавиатура" and planned["desktop"]["tz"] == "ТЗ 14"
    assert planned["minecraft"]["title"] == "Minecraft" and planned["minecraft"]["tz"] == ""
    assert "status" not in planned["montage"] and "enabled" not in planned["montage"]


async def test_echo_is_hidden_without_dev_mode_and_shown_with_it():
    wire()
    assert settings.load()["dev_mode"] is False
    assert "echo" not in {r["id"] for r in await rows()}
    settings.update(dev_mode=True)
    echo = next(r for r in await rows() if r["id"] == "echo")
    assert echo["kind"] == "module" and echo["title"] == "Пример модуля (для разработчиков)"
    assert echo["description"] == "повторяет текст; проверка протокола модулей"
    settings.update(dev_mode=False)
    assert "echo" not in {r["id"] for r in await rows()}


def test_planned_modules_are_not_run_by_the_supervisor():
    layer = wire()
    planned = {p.id for p in load_planned()}
    assert planned == {"montage", "desktop", "minecraft"}
    assert not planned & {s.id for s in load_specs()}                                    # в modules они не попадают
    assert not planned & set(layer.supervisor.specs)
    assert not planned & set(layer.modules.records)
    assert all(layer.supervisor.status(module_id) is None for module_id in planned)


def test_dev_mode_is_a_setting_with_a_form_field():
    assert settings.DEFAULTS["dev_mode"] is False
    html = (AGENT / "static" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "key: 'dev_mode'" in html and "id=\"core-list\"" in html


def test_load_planned_skips_broken_entries(tmp_path):
    path = tmp_path / "modules.yaml"
    path.write_text("modules: []\nplanned:\n  - {id: a, title: А, tz: ТЗ 1}\n  - {id: b}\n  - just text\n  - {id: c, title: С}\n",
                    encoding="utf-8")
    assert [(p.id, p.title, p.tz) for p in load_planned(path)] == [("a", "А", "ТЗ 1"), ("c", "С", "")]
