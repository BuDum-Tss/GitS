"""Фокус среды (`services/spaces/focus.py`, бывшие профили): тон, видимые модули и области памяти следуют за текущей средой."""
from types import SimpleNamespace

from services.spaces.focus import SpaceConfig, SpaceFocus, load_config


def make(current="assistant", streamer=False, config=None):
    ws = SimpleNamespace(current=current, specs={"rimworld": SimpleNamespace(title="RimWorld", purpose="колония"),
                                                  "assistant": SimpleNamespace(title="Ассистент", purpose="разговор")})
    return ws, SpaceFocus(ws, config=config if config is not None else load_config()[0], streamer_tone="Для зрителей.",
                          streamer=lambda: streamer)


def test_config_file_has_the_old_profiles_data():
    spaces, streamer_tone = load_config()
    assert set(spaces) >= {"assistant", "sleep", "rimworld", "montage"} and streamer_tone
    assert spaces["assistant"].focus == ("desktop", "telegram") and spaces["rimworld"].memory_scopes == ("rimworld",)


def test_focus_follows_the_current_workspace():
    ws, f = make()
    assert f.focus_modules() == {"desktop", "telegram"} and f.memory_scopes() == ["assistant"]
    ws.current = "rimworld"
    assert f.focus_modules() == {"rimworld"} and f.info() == {"id": "rimworld", "title": "RimWorld", "focus_modules": ["rimworld"]}
    ws.current = "sleep"
    assert f.focus_modules() == set()


def test_unknown_module_space_shows_itself_and_has_no_tone():
    ws, f = make("plain")
    assert f.focus_modules() == {"plain"} and f.memory_scopes() == ["plain"]
    assert "Внутренняя настройка тона" in f.hint()


def test_hint_has_the_tone_and_the_streamer_addition():
    ws, f = make("rimworld", streamer=False)
    assert "мои колонисты" in f.hint() and "Для зрителей." not in f.hint()
    ws, f = make("rimworld", streamer=True)
    assert "Для зрителей." in f.hint() and "Никогда не упоминай режимы" in f.hint()


def test_scope_title_uses_the_workspace_that_opens_it():
    ws, f = make(config={"rimworld": SpaceConfig(memory_scopes=("rimworld",))})
    assert f.scope_title("rimworld") == "колония" and f.scope_title("чужая") == "чужая"
