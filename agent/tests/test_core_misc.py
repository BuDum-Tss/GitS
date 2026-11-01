"""Мелочи ТЗ 02: настройки modules (CORE-40), инструмент set_profile, видимость старых манифестов, планировщик и задачи."""
import asyncio

import pytest
from hub_helpers import FakeCompanion

import settings
from environments import builtin
from environments.base import ToolError
from environments.registry import Registry
from profile_manager import ProfileManager
from router import Router
from user_memory import UserMemory


def test_modules_setting_merges_one_level_deep():
    assert settings.load()["modules"] == {}
    settings.update(modules={"echo": {"enabled": True}})
    settings.update(modules={"echo": {"poll_seconds": 9}, "voice": {"enabled": False}})
    assert settings.load()["modules"] == {"echo": {"enabled": True, "poll_seconds": 9}, "voice": {"enabled": False}}
    settings.update(modules={"echo": {"enabled": False}})
    assert settings.load()["modules"]["echo"] == {"enabled": False, "poll_seconds": 9}
    settings.update(volume=50)                                  # прочие настройки не трогают modules
    assert settings.load()["modules"]["voice"] == {"enabled": False}


def test_defaults_are_not_shared_between_loads():
    a = settings.load()
    a["modules"]["x"] = {"enabled": True}
    assert settings.load()["modules"] == {} and settings.DEFAULTS["modules"] == {}


def test_modules_setting_survives_broken_value(tmp_path):
    (tmp_path / "settings.json").write_text('{"modules": 5, "volume": 10}', encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["modules"] == {} and settings.load()["volume"] == 10


def test_profile_setting_default_is_auto():
    assert settings.load()["profile"] == "auto"


class _Dummy:
    def __getattr__(self, name):
        return None


async def test_set_profile_tool_is_rebuilt_on_profiles(tmp_path):
    reg = Registry()
    pm = ProfileManager(battery=lambda: {}, foreground_app=lambda: None)
    pm.evaluate()
    builtin.register(reg, FakeCompanion(), UserMemory(str(tmp_path)), profiles=pm)
    tool = reg.get("assistant.set_profile")
    assert tool.params["mode"]["enum"] == pm.choices() and "planned" not in tool.description
    assert set(tool.params["mode"]["enum"]) == {"auto", "assistant", "work", "rimworld", "editing", "sleep"}
    assert sum(t.name == "set_profile" for e in reg.envs.values() for t in e.tools) == 1      # не дублируется
    assert await tool.call({"mode": "work"}) == "профиль: Работа и код"
    assert settings.load()["profile"] == "work" and pm.current_id == "work"
    assert "автоматически" in await tool.call({"mode": "auto"})
    with pytest.raises(ToolError):
        await tool.call({"mode": "minecraft"})                  # ещё не написан
    ok, _name, info, _code = await Router(reg, FakeCompanion().say)._step(
        {"tool": "assistant.set_profile", "args": {"mode": "game"}}, {"assistant.set_profile": tool})
    assert not ok and "одним из" in info                        # enum из файлов профилей, прежних значений в нём нет


def test_core_tools_exist_and_are_always_visible():
    reg = Registry()

    class Mods:
        def jobs_all(self):
            return []

        async def cancel_job(self, module, job_id):
            return "ок"
    builtin.register_core_tools(reg, Mods())
    reg.focus = lambda: set()
    assert {t.id for t in reg.active_tools()} == {"core.jobs", "core.job_cancel", "core.set_setting", "core.set_character", "core.modules_status", "core.module_events"}
    assert reg.get("core.jobs").risk == "auto" and reg.get("core.jobs").informative
    assert reg.get("core.job_cancel").risk == "ask"
    assert asyncio.run(reg.get("core.jobs").call({})) == "задач у модулей сейчас нет"


async def test_job_listing_format():
    reg = Registry()

    class Mods:
        def jobs_all(self):
            return [{"module": "montage", "job_id": "j-00000001", "title": "Шортс", "state": "running", "stage": "render", "progress": 0.35}]

        async def cancel_job(self, module, job_id):
            return "ок"
    builtin.register_core_tools(reg, Mods())
    text = await reg.get("core.jobs").call({})
    assert "montage j-00000001: Шортс, running, стадия render, 35%" in text


async def test_legacy_manifest_environments_become_profile_only(tmp_path):
    """Старый манифест (rimworld.json) с module != core: инструменты видны только когда модуль в фокусе профиля."""
    import json
    manifest = {"id": "game", "title": "Игра", "module": "game", "enabled": True, "sources": [
        {"type": "webhook_out", "name": "ping", "url": "http://127.0.0.1:1/x", "params": {}, "risk": "auto"}]}
    (tmp_path / "game.json").write_text(json.dumps(manifest), encoding="utf-8")
    manifest.update(id="plain", module="core")
    (tmp_path / "plain.json").write_text(json.dumps(manifest), encoding="utf-8")
    reg = Registry()
    await reg.load_manifests(tmp_path)
    reg.focus = lambda: set()
    assert [t.id for t in reg.active_tools()] == ["plain.ping"]            # module=core: виден всегда
    reg.focus = lambda: {"game"}
    assert {t.id for t in reg.active_tools()} == {"plain.ping", "game.ping"}
    assert reg.get("game.ping").availability == "profile" and reg.get("plain.ping").availability == "always"


async def test_permission_question_is_a_fact_without_the_tool_description():
    """Вопрос о разрешении уходит модели фактом: в нём нет описания инструмента (оно написано для агента: «используй, только если…»)."""
    from types import SimpleNamespace

    said = []

    async def say(text):
        said.append(text)

    class Approver:
        async def request(self, text, announce, danger=None):
            await announce(text)
            return True

    async def call(args):
        return "ok"
    tool = SimpleNamespace(id="desktop.click_text", env="desktop", name="click_text", params={"text": {"type": "string", "description": "надпись"}}, danger=None, danger_check=False,
                           check=None, risk="ask", self_confirming=False, timeout_s=5, informative=False,
                           description="ЗАПАСНОЙ УРОВЕНЬ: используй, только если у программы нет прямого инструмента.", call=call)
    router = Router(SimpleNamespace(), say, approver=Approver())
    ok, *_ = await router._step({"tool": "desktop.click_text", "args": {"text": "Работа"}}, {"desktop.click_text": tool})
    assert ok and len(said) == 1
    assert "click_text" in said[0] and "text=Работа" in said[0] and "ЗАПАСНОЙ" not in said[0] and "используй" not in said[0]


async def test_agent_playing_in_an_autonomous_environment_is_not_asking_for_ordinary_permission():
    """Пока агент играет сам (автономная среда), действие с риском ask идёт без вопроса; опасное всё равно спрашивается."""
    from types import SimpleNamespace

    asked = []

    class Approver:
        async def request(self, text, announce, danger=None):
            asked.append((text, danger))
            return True

    async def call(args):
        return "ok"

    def tool(danger=None):
        return SimpleNamespace(id="desktop.click_text", env="desktop", name="click_text", params={}, danger=(lambda a: danger),
                               danger_check=False, check=None, risk="ask", self_confirming=False, timeout_s=5, informative=False,
                               description="d", call=call)
    router = Router(SimpleNamespace(), lambda t: None, approver=Approver())
    router.auto_ok = lambda: True
    ok, *_ = await router._step({"tool": "desktop.click_text", "args": {}}, {"desktop.click_text": tool()})
    assert ok and asked == []
    ok, *_ = await router._step({"tool": "desktop.click_text", "args": {}}, {"desktop.click_text": tool("удаление файла")})
    assert ok and len(asked) == 1                                      # опасное спрашивается всегда
    router.auto_ok = lambda: False
    asked.clear()
    ok, *_ = await router._step({"tool": "desktop.click_text", "args": {}}, {"desktop.click_text": tool()})
    assert ok and len(asked) == 1                                      # вне игры всё как раньше
