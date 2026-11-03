"""Переключатель камеры: главный выключатель в моде (кнопка в игре), модуль только передаёт моду настройку `camera_agent`."""
import asyncio

import controller


def make(setting: bool, game_up: bool):
    c = controller.Controller.__new__(controller.Controller)
    c.settings_now = {"camera_agent": setting}
    c._module_setting = lambda key, default: c.settings_now.get(key, default)
    c._game_up = game_up
    return c


def run(c, sent):
    asyncio.run(c._sync_camera_switch())
    return list(sent)


def test_setting_is_pushed_to_the_mod_only_when_it_changes_or_the_game_starts_with_it_off(monkeypatch):
    sent = []
    monkeypatch.setattr(controller, "_camera_command", lambda cmd, **k: sent.append(cmd))
    c = make(True, True)
    assert run(c, sent) == []                                            # старт с включённым: в моде и так включено
    c.settings_now["camera_agent"] = False
    assert run(c, sent) == [{"type": "set_agent_camera", "enabled": False}]
    assert run(c, sent) == [{"type": "set_agent_camera", "enabled": False}]     # без изменений второй раз не шлём
    c.settings_now["camera_agent"] = True
    assert run(c, sent)[-1] == {"type": "set_agent_camera", "enabled": True}


def test_nothing_is_sent_while_the_game_is_down_and_the_off_setting_follows_the_game_start(monkeypatch):
    sent = []
    monkeypatch.setattr(controller, "_camera_command", lambda cmd, **k: sent.append(cmd))
    c = make(False, False)
    assert run(c, sent) == []
    c._game_up = True                                                    # игра запустилась с выключенной настройкой
    assert run(c, sent) == [{"type": "set_agent_camera", "enabled": False}]
