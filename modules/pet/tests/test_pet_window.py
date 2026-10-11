"""Окно питомца на настоящем tkinter, но скрытое (withdraw): рисование, позиция, перетаскивание и клики; на экране ничего не появляется."""
import queue

import pytest

import pet_window
from pet_logic import PetController, PetState


@pytest.fixture
def window():
    try:
        sprites = pet_window.load_sprites()
        posted = []
        state = PetState()
        win = pet_window.PetWindow(lambda: state, sprites, post=posted.append)
    except Exception as e:                                    # нет экрана (сервер сборки): окно проверить негде
        pytest.skip(f"tkinter недоступен: {e}")
    win.state_obj, win.posted = state, posted
    yield win
    win.root.destroy()


def test_sprites_are_loaded_for_every_sticker():
    sprites, w, h = pet_window.load_sprites()
    assert set(sprites) == set(pet_window.STICKER_MAP) and h == pet_window.STICKER_H and w > 0


def test_render_frame_grows_with_the_bubble_text():
    sprites, w, h = pet_window.load_sprites()
    _img, h0 = pet_window.render_frame(sprites, w, h, "idle", "", False, bubble_visible=False)
    _img, h1 = pet_window.render_frame(sprites, w, h, "happy", "Коротко.", False, bubble_visible=True)
    _img, h2 = pet_window.render_frame(sprites, w, h, "happy", "Очень длинное предложение. " * 10, False, bubble_visible=True)
    assert h0 < h1 < h2
    _img, h3 = pet_window.render_frame(sprites, w, h, "нет-такого", "x", False)                # неизвестный стикер: idle
    assert h3 > 0


def test_window_stays_hidden_until_told_and_draws_without_showing(window):
    window.draw("angry", "Облачко с текстом.", True)
    window.draw("resting", "", False)
    window.set_visible(False)
    window.root.update()
    assert window.root.state() == "withdrawn"


def test_position_from_settings_is_clamped_to_the_screen_once(window):
    window.on_settings({"pet_x": 10**6, "pet_y": 10**6})
    window.root.update_idletasks()
    sw, sh = window._screen
    assert window.root.winfo_x() <= sw - pet_window.W and window.root.winfo_y() <= sh
    x_before = window.root.winfo_x()
    window.on_settings({"pet_x": 0, "pet_y": 0})                 # позиция применяется один раз: дальше окно двигает пользователь
    window.root.update_idletasks()
    assert window.root.winfo_x() == x_before


class Ev:
    def __init__(self, x, y):
        self.x_root, self.y_root = x, y


def test_drag_saves_the_position_and_pinned_window_does_not_move(window):
    window.root.geometry(f"{pet_window.W}x{window.H}+100+100")
    window.root.update_idletasks()
    window._on_press(Ev(150, 150))
    window._on_drag(Ev(190, 170))
    window._on_release(Ev(190, 170))
    assert window.posted and set(window.posted[-1]) == {"pet_x", "pet_y"}
    window.posted.clear()
    window.state_obj.apply("settings", {"pet_pinned": True}, 0.0)
    before = (window.root.winfo_x(), window.root.winfo_y())
    window._on_press(Ev(150, 150))
    window._on_drag(Ev(400, 400))
    window._on_release(Ev(400, 400))
    window.root.update_idletasks()
    assert (window.root.winfo_x(), window.root.winfo_y()) == before and window.posted == []


def test_right_click_hides_and_hotkey_mute_toggles_through_the_dashboard(window):
    window._hide_pet()
    assert window.posted[-1] == {"pet_visible": False}
    window.toggle_mute()
    assert window.posted[-1] == {"voice_muted": True}
    window.state_obj.apply("settings", {"voice_muted": True}, 0.0)
    window.toggle_mute()
    assert window.posted[-1] == {"voice_muted": False}


def test_controller_drives_the_real_window_through_the_queue(window):
    q: queue.Queue = queue.Queue()
    c = PetController(window, q, on_settings=window.on_settings)
    window.state = lambda: c.state
    q.put(("connected", True))
    q.put(("settings", {"pet_visible": False}))                  # скрытое остаётся скрытым
    q.put(("presence", {"speaking": True, "segment": "Привет.", "emotion": "happy", "emotion_ts": 1e12}))
    assert c.pump() == 3
    window.root.update()
    assert window.root.state() == "withdrawn"
    class StubInput:                                              # настоящая строка ввода открыла бы окно на экране
        toggled = 0

        def toggle(self):
            self.toggled += 1
    window.qi = StubInput()
    window.input_requests.put(1)
    window.pump = c.pump
    window._tick()                                                # просьба открыть строку ввода и разбор очереди
    assert window.qi.toggled == 1 and window.input_requests.empty()
