"""Вкладка «Состояние»: карточка состояния только для чтения (без переключателей режима) и буфер событий."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "ui" / "index.html").read_text(encoding="utf-8")


def test_profile_graph_is_gone():
    for gone in ("modes-canvas", "modes-back", "modes-fit", "modes-detail", "profileStyle", "modesElements", "renderToolDetail",
                 "MD.cy", "MD.view"):
        assert gone not in HTML, gone


def test_state_card_only_shows_the_state():
    for kept in ('id="now-box"', 'id="st-env"', 'id="st-profile"', 'id="st-speech"', 'id="st-game"', 'id="now-goal"',
                 'id="now-meta"', 'id="now-last"', 'id="modes-battery"', 'id="badge-battery"', "function renderNow",
                 "function batteryHtml", "function batteryWhen"):
        assert kept in HTML, kept
    for gone in ('id="modes-select"', 'id="now-assistant-btn"', 'id="modes-streamer"', "/api/mode'", "Выбирает сама",
                 'id="g-prio"', 'id="g-sched"', 'id="g-appr"'):
        assert gone not in HTML, gone                 # ни выбора режима, ни кнопок возврата, ни таблиц-органов управления игрой


def test_event_buffer_is_on_the_state_tab_and_diagnostics_are_folded():
    for kept in ('id="buf-list"', 'id="buf-next"', 'id="buf-legend"', "function loadBuffer", "/api/buffer", "в контексте"):
        assert kept in HTML, kept
    assert '<details id="diag-box"' in HTML and '<details id="caps-box"' in HTML        # поток данных и возможности свёрнуты


def test_graph_helpers_stay_for_memory_and_scheme():
    assert "function makeGraph" in HTML and "function syncGraph" in HTML and "function fitGraph" in HTML


def test_focus_block_is_gone_and_game_panel_stays():
    assert 'id="focus-box"' not in HTML and "loadFocus" not in HTML and "/api/focus" not in HTML
    assert 'id="panel-rimworld"' in HTML and 'id="caps-list"' in HTML
