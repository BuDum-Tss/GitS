"""Смена приложения и простой (ТЗ 14, раздел 5) на подменённых часах: без экрана и ввода."""
from watch import APP_DEBOUNCE_S, IDLE_AWAY_S, FocusWatch


def win(app, title="окно"):
    return {"app": app, "title": title}


def test_first_app_is_remembered_without_event():
    w = FocusWatch()
    assert w.tick(1000, win("code", "proj"), 0) == []
    assert w.app == "code"


def test_app_change_is_reported_with_title_hint():
    w = FocusWatch()
    w.tick(1000, win("code"), 0)
    evs = w.tick(1000 + APP_DEBOUNCE_S, win("chrome", "Почта"), 0)
    assert [e["kind"] for e in evs] == ["desktop.app_changed"]
    assert evs[0]["data"] == {"app": "chrome", "title_hint": "Почта"}


def test_app_change_is_debounced_and_flicker_is_not_reported():
    w = FocusWatch()
    w.tick(1000, win("code"), 0)                       # стартовое приложение, отметка 1000
    assert w.tick(1001, win("chrome"), 0) == []        # менее 3 с с отметки: пока молчим
    assert w.tick(1002, win("code"), 0) == []          # вернулись к сообщённому приложению: смены нет
    evs = w.tick(1003, win("chrome"), 0)               # 3 с с отметки прошли: смена сообщается
    assert [e["kind"] for e in evs] == ["desktop.app_changed"] and evs[0]["data"]["app"] == "chrome"


def test_title_is_hidden_and_cut_for_sensitive_window():
    w = FocusWatch()
    w.tick(1000, win("notepad"), 0)
    evs = w.tick(1010, {"app": "keepass", "title": "База паролей"}, 0, sensitive=True)
    assert evs[0]["data"] == {"app": "keepass", "title_hint": None}
    long_title = "x" * 200
    w2 = FocusWatch()
    w2.tick(0, win("a"), 0)
    evs = w2.tick(10, {"app": "b", "title": long_title}, 0)
    assert len(evs[0]["data"]["title_hint"]) == 80


def test_idle_goes_away_and_back_with_the_idle_length_before_return():
    w = FocusWatch()
    w.tick(0, win("code"), 0)
    assert w.tick(10, win("code"), IDLE_AWAY_S - 1) == []
    evs = w.tick(20, win("code"), IDLE_AWAY_S + 5)
    assert [e["kind"] for e in evs] == ["desktop.user_idle"] and evs[0]["data"]["idle_s"] == IDLE_AWAY_S + 5
    assert w.tick(30, win("code"), IDLE_AWAY_S + 7) == []      # пока отошла, повторно не шлём
    evs = w.tick(40, win("code"), 3)
    assert [e["kind"] for e in evs] == ["desktop.user_back"] and evs[0]["data"]["idle_s"] == round(IDLE_AWAY_S + 7)


def test_no_window_is_fine():
    w = FocusWatch()
    assert w.tick(0, None, 0) == []
    assert w.app is None
