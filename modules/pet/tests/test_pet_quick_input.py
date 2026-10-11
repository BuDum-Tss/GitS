"""Быстрый ввод (PET-7): разбор горячей клавиши и адрес пульта управления."""
import pytest

import pet_logic
import quick_input


def test_parse_hotkey():
    assert quick_input.parse_hotkey("Ctrl+Shift+Space") == (0x2 | 0x4, 0x20)
    assert quick_input.parse_hotkey("alt+space") == (0x1, 0x20)
    assert quick_input.parse_hotkey("Ctrl+Alt+A") == (0x2 | 0x1, ord("A"))
    assert quick_input.parse_hotkey("Win+Shift+f5") == (0x8 | 0x4, 0x6F + 5)
    assert quick_input.parse_hotkey("Control+1") == (0x2, ord("1"))


@pytest.mark.parametrize("spec", ["Space", "Ctrl+", "Ctrl+Shift", "Ctrl+Эй", "Ctrl+F13", "Ctrl+Alt+Блин", ""])
def test_parse_hotkey_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        quick_input.parse_hotkey(spec)


def test_dashboard_base_follows_the_core_url(monkeypatch):
    monkeypatch.delenv("GLADOS_CORE_URL", raising=False)
    assert pet_logic.dashboard_base() == "http://127.0.0.1:8766"
    monkeypatch.setenv("GLADOS_CORE_URL", "ws://127.0.0.1:9100/module/ws")
    assert pet_logic.dashboard_base() == "http://127.0.0.1:9100"
    monkeypatch.setenv("GLADOS_DASHBOARD_URL", "http://localhost:7000/")
    assert pet_logic.dashboard_base() == "http://localhost:7000"


def test_quick_input_uses_the_chat_and_core_endpoints():
    assert quick_input.CHAT_URL.endswith("/api/chat") and quick_input.CORE_URL.endswith("/api/core")
