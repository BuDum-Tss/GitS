"""Профили поведения: определение по окну, ручной выбор, удержание после окна без признаков."""
import profiles
import settings


def test_detects_by_app_and_sticks_briefly(tmp_path):
    settings.init(str(tmp_path))
    profiles._last.update(id="general", ts=0.0)
    assert profiles.detect("Code", now=1000).id == "work"
    assert profiles.detect("chrome", now=1060).id == "work"          # браузер: держим прежний
    assert profiles.detect("chrome", now=1000 + 500).id == "general"  # давно: обычный
    assert profiles.detect("RimWorldWin64", now=2000).id == "game"


def test_manual_choice_overrides_window(tmp_path):
    settings.init(str(tmp_path))
    settings.update(profile="stream")
    assert profiles.current("Code").id == "stream"
    settings.update(profile="auto")
    assert profiles.current("Code").id == "work"
