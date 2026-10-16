"""Вкладка «Состояние»: вместо графа профилей карточка «Сейчас» (чем агент занят)."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "ui" / "index.html").read_text(encoding="utf-8")


def test_profile_graph_is_gone():
    for gone in ("modes-canvas", "modes-back", "modes-fit", "modes-detail", "profileStyle", "modesElements", "renderToolDetail",
                 "MD.cy", "MD.view"):
        assert gone not in HTML, gone


def test_now_card_has_its_controls():
    for kept in ('id="now-box"', 'id="now-goal"', 'id="now-meta"', 'id="now-last"', 'id="now-assistant-btn"',
                 'id="now-pin-btn"', 'id="now-dnd-btn"', 'id="modes-select"', 'id="modes-streamer"', 'id="modes-battery"',
                 'id="badge-battery"', "function renderNow", "function batteryHtml", "function batteryWhen"):
        assert kept in HTML, kept


def test_graph_helpers_stay_for_memory_and_scheme():
    assert "function makeGraph" in HTML and "function syncGraph" in HTML and "function fitGraph" in HTML


def test_focus_details_and_game_panel_stay():
    assert '<details id="focus-box">' in HTML
    assert 'id="panel-rimworld"' in HTML and 'id="caps-list"' in HTML
