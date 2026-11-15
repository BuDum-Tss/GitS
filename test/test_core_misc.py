"""Мелочи ТЗ 02: настройки peers (CORE-40, прежний ключ modules переносится), инструмент set_profile, видимость старых манифестов, планировщик и задачи."""
import asyncio

from hub_helpers import FakeCompanion

from host import settings
from environments import builtin
from environments.registry import Registry
from services.tools.router import Router
from services.memory_service.user import UserMemory


def test_peers_setting_merges_one_level_deep():
    assert settings.load()["peers"] == {}
    settings.update(peers={"echo": {"enabled": True}})
    settings.update(peers={"echo": {"poll_seconds": 9}, "voice": {"enabled": False}})
    assert settings.load()["peers"] == {"echo": {"enabled": True, "poll_seconds": 9}, "voice": {"enabled": False}}
    settings.update(peers={"echo": {"enabled": False}})
    assert settings.load()["peers"]["echo"] == {"enabled": False, "poll_seconds": 9}
    settings.update(volume=50)                                  # прочие настройки не трогают peers
    assert settings.load()["peers"]["voice"] == {"enabled": False}


def test_defaults_are_not_shared_between_loads():
    a = settings.load()
    a["peers"]["x"] = {"enabled": True}
    assert settings.load()["peers"] == {} and settings.DEFAULTS["peers"] == {}


def test_peers_setting_survives_broken_value(tmp_path):
    (tmp_path / "settings.json").write_text('{"peers": 5, "volume": 10}', encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {} and settings.load()["volume"] == 10


def test_legacy_modules_key_is_migrated_to_peers_once(tmp_path):
    import json
    path = tmp_path / "settings.json"
    path.write_text('{"modules": {"echo": {"enabled": false, "poll": 3}}, "volume": 10}', encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {"echo": {"enabled": False, "poll": 3}} and settings.load()["volume"] == 10
    saved = json.loads(path.read_text(encoding="utf-8"))                # файл переписан: старого ключа нет
    assert "modules" not in saved and saved["peers"]["echo"]["poll"] == 3
    settings.update(peers={"echo": {"enabled": True}})
    assert settings.load()["peers"]["echo"] == {"enabled": True, "poll": 3}


def test_existing_peers_key_wins_over_legacy_modules(tmp_path):
    (tmp_path / "settings.json").write_text('{"modules": {"a": {"enabled": false}}, "peers": {"b": {"enabled": false}}}',
                                            encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {"b": {"enabled": False}}


def test_twitch_and_obs_peers_are_merged_into_streamer_once(tmp_path):
    import json
    path = tmp_path / "settings.json"
    path.write_text('{"peers": {"twitch": {"enabled": false, "owner_login": "me", "chat_events_per_min": 5}, '
                    '"obs": {"enabled": true}, "echo": {"enabled": false}}}', encoding="utf-8")
    settings.init(str(tmp_path))
    peers = settings.load()["peers"]
    assert peers == {"streamer": {"enabled": True, "owner_login": "me", "chat_events_per_min": 5}, "echo": {"enabled": False}}
    saved = json.loads(path.read_text(encoding="utf-8"))                # файл переписан: старых ключей нет
    assert "twitch" not in saved["peers"] and "obs" not in saved["peers"] and "streamer" in saved["peers"]


def test_streamer_migration_both_disabled_and_existing_streamer_wins(tmp_path):
    (tmp_path / "settings.json").write_text('{"peers": {"twitch": {"enabled": false}, "obs": {"enabled": false}}}', encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {"streamer": {"enabled": False}}
    (tmp_path / "settings.json").write_text('{"peers": {"twitch": {"enabled": false}, "streamer": {"enabled": true, "x": 1}}}',
                                            encoding="utf-8")
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {"streamer": {"enabled": True, "x": 1}}
    (tmp_path / "settings.json").write_text('{"modules": {"obs": {"enabled": false}}}', encoding="utf-8")   # через прежний ключ modules
    settings.init(str(tmp_path))
    assert settings.load()["peers"] == {"streamer": {"enabled": False}}


def test_peer_entry_reads_only_peers_key():
    cfg = {"peers": {"a": {"enabled": False}}, "modules": {"a": {"enabled": True}, "b": {"enabled": False}}}
    assert settings.peer_entry(cfg, "a") == {"enabled": False}
    assert settings.peer_entry(cfg, "b") is None                        # прежний ключ modules больше не читается
    assert settings.peer_entry(cfg, "c") is None


def test_profile_setting_is_gone():
    assert "profile" not in settings.DEFAULTS


class _Dummy:
    def __getattr__(self, name):
        return None


def test_core_tools_exist_and_are_always_visible():
    reg = Registry()

    class Mods:
        def jobs_all(self):
            return []

        async def cancel_job(self, module, job_id):
            return "ок"
    builtin.register_core_tools(reg, Mods())
    reg.focus = lambda: set()
    assert {t.id for t in reg.active_tools()} == {"core.resources", "core.jobs", "core.job_cancel", "core.set_setting", "core.modules_status", "core.module_events"}
    assert reg.get("core.jobs").risk == "auto" and reg.get("core.jobs").informative
    assert reg.get("core.job_cancel").risk == "auto"
    assert asyncio.run(reg.get("core.jobs").call({})) == "задач у окружений сейчас нет"


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


async def test_publishing_never_asks_for_permission_in_the_core():
    """Посты в канал разрешения не просят: ни риск public, ни старая настройка confirm в peers.telegram на это не влияют."""
    from types import SimpleNamespace

    asked = []

    class Approver:
        async def request(self, text, announce, danger=None):
            asked.append(text)
            return False

    class Settings:
        @staticmethod
        def load():
            return {"peers": {"telegram": {"confirm": True}}}      # устаревшее значение игнорируется

    async def call(args):
        return "опубликовано"

    tool = SimpleNamespace(id="telegram.message", env="telegram", name="message", params={}, danger=None, danger_check=False,
                           check=None, risk="public", self_confirming=False, timeout_s=5, informative=True,
                           description="d", call=call)
    router = Router(SimpleNamespace(), lambda t: None, approver=Approver(), settings_provider=Settings)
    ok, _name, result, _code = await router._step({"tool": "telegram.message", "args": {}}, {"telegram.message": tool})
    assert ok and result == "опубликовано" and asked == []
    assert not hasattr(router, "_publish_confirm")


def test_pet_settings_migrate_from_core_settings_to_peers_pet(tmp_path):
    """Настройки питомца раньше лежали в общих настройках ядра; теперь это собственные настройки участника `peers.pet`."""
    import json
    from host import settings
    (tmp_path / "settings.json").write_text(json.dumps({
        "pet_visible": False, "pet_pinned": True, "pet_bubble_secs": 40, "pet_x": 120, "pet_y": None,
        "hotkey_input": "Alt+Shift+Space", "volume": 70}), encoding="utf-8")
    settings.init(str(tmp_path))
    data = settings.load()
    assert data["volume"] == 70 and not any(k.startswith("pet_") for k in data) and "hotkey_input" not in data
    assert data["peers"]["pet"] == {"visible": False, "pinned": True, "bubble_secs": 40, "x": 120, "hotkey_input": "Alt+Shift+Space"}
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "pet_visible" not in saved and saved["peers"]["pet"]["pinned"] is True        # файл переписан
    settings.update(peers={"pet": {"visible": True}})
    assert settings.load()["peers"]["pet"]["visible"] is True and settings.load()["peers"]["pet"]["x"] == 120


def test_telegram_settings_migrate_from_core_settings_to_peers_telegram(tmp_path):
    """Настройки Telegram раньше лежали в общих настройках ядра; теперь это собственные настройки окружения `peers.telegram`."""
    import json
    from host import settings
    (tmp_path / "settings.json").write_text(json.dumps({
        "telegram": True, "telegram_after_run": True, "telegram_voice": False, "telegram_confirm": True, "volume": 70,
        "peers": {"telegram": {"owner_tg_id": 5, "voice": True}}}), encoding="utf-8")
    settings.init(str(tmp_path))
    data = settings.load()
    assert data["volume"] == 70 and not any(k.startswith("telegram") for k in data)
    assert data["peers"]["telegram"] == {"owner_tg_id": 5, "voice": True, "publish": True, "after_run": True}   # telegram_confirm отброшен; своё не затирается
    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "telegram" not in saved and saved["peers"]["telegram"]["publish"] is True        # файл переписан


def test_profile_tool_is_gone_and_scope_follows_the_workspace():
    reg = Registry()
    builtin.register(reg, FakeCompanion(), UserMemory("."))
    assert reg.get("assistant.set_profile") is None


def test_agent_can_mute_the_desktop_voice_but_unmuting_needs_the_owner():
    """Заглушку озвучки на ПК ставит владелец: она сама её не снимает (раньше сама включала озвучку «чтобы говорить в войсчате»).
    Заглушить себя она может без вопроса; вернуть звук только с разрешения владельца."""
    reg = Registry()
    builtin.register(reg, FakeCompanion(), UserMemory("."))
    tool = reg.get("assistant.set_voice")
    assert tool.danger({"muted": True}) is None
    reason = tool.danger({"muted": False})
    assert reason and "владельц" in reason
    assert "voice.say" in tool.description and "Discord" in tool.description          # не путает ПК-колонки с голосовыми каналами
