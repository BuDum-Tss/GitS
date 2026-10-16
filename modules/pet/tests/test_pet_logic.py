"""Логика питомца без окна (PET-4, PET-5, PET-8, PET-12): стикер и облачко по присутствию и времени, связка очереди с окном."""
import queue

import pet_logic as pl
from pet_logic import PetController, PetState, sticker_for


def pres(**kw):
    return {"speaking": False, "utterance_id": None, "segment": "", "emotion": "idle", "emotion_ts": 0.0, **kw}


# ── стикер (PET-5) ───────────────────────────────────────────────────────────

def test_sticker_is_the_fresh_emotion_else_idle():
    assert sticker_for(pres(emotion="angry", emotion_ts=1000.0), now=1010.0) == "angry"
    assert sticker_for(pres(emotion="angry", emotion_ts=1000.0), now=1000.0 + pl.EMOTION_TTL_SECS + 1) == "idle"
    assert sticker_for(pres(emotion="angry", emotion_ts=1000.0), now=1000.0 + pl.EMOTION_TTL_SECS - 1) == "angry"
    assert sticker_for(pres(emotion="нет-такой", emotion_ts=1000.0), now=1001.0) == "idle"
    assert sticker_for({}, now=5.0) == "idle"
    assert sticker_for(pres(emotion="happy", emotion_ts="плохо"), now=1.7e9) == "idle"        # испорченное время не роняет


def test_sticker_is_resting_when_the_core_is_unreachable():
    assert sticker_for(pres(emotion="angry", emotion_ts=1000.0), now=1001.0, connected=False) == "resting"
    s = PetState()
    s.apply("presence", pres(emotion="sad", emotion_ts=100.0), 101.0)
    assert s.sticker(101.0) == "resting"                       # клиент не подключён
    s.apply("connected", True, 101.0)
    assert s.sticker(101.0) == "sad"
    s.apply("connected", False, 102.0)
    assert s.sticker(102.0) == "resting"


def test_every_sticker_name_has_a_file():
    from gits_sdk import resources
    stickers = resources.module_dir("pet") / "stickers"
    for name in pl.STICKER_MAP.values():
        assert (stickers / f"{name}.png").exists(), name


# ── облачко (PET-4) ──────────────────────────────────────────────────────────

def test_bubble_shows_the_current_segment_while_speaking():
    s = PetState()
    s.apply("presence", pres(speaking=True, segment="Первое предложение."), 100.0)
    assert s.bubble(100.0) == ("Первое предложение.", True)
    assert s.bubble(500.0) == ("Первое предложение.", True)     # пока она говорит, облачко не гаснет
    s.apply("presence", pres(speaking=True, segment="Второе предложение."), 103.0)
    assert s.bubble(103.0) == ("Второе предложение.", True)


def test_bubble_holds_the_last_segment_after_speech_then_hides():
    s = PetState()
    s.apply("presence", pres(speaking=True, segment="Последнее."), 100.0)
    s.apply("presence", pres(speaking=False, segment=""), 110.0)          # речь кончилась в 110
    assert s.bubble(110.0) == ("Последнее.", True)
    assert s.bubble(110.0 + pl.BUBBLE_HIDE_SECS - 1) == ("Последнее.", True)
    assert s.bubble(110.0 + pl.BUBBLE_HIDE_SECS + 1) == ("", False)


def test_bubble_is_hidden_before_any_speech_and_survives_other_presence_changes():
    s = PetState()
    assert s.bubble(1.0) == ("", False)
    s.apply("presence", pres(emotion="happy", emotion_ts=1.0), 1.0)
    assert s.bubble(1.0) == ("", False)
    s.apply("presence", pres(speaking=True, segment="Фраза."), 2.0)
    s.apply("presence", pres(speaking=False, segment="", emotion="angry", emotion_ts=3.0), 4.0)
    s.apply("presence", pres(speaking=False, segment="", busy=True), 20.0)       # не конец речи: удержание не продлевается
    assert s.bubble(4.0 + pl.BUBBLE_HIDE_SECS - 1) == ("Фраза.", True)
    assert s.bubble(4.0 + pl.BUBBLE_HIDE_SECS + 1) == ("", False)


def test_core_without_voice_sends_the_whole_text_as_segment():
    s = PetState()
    s.apply("presence", pres(speaking=True, segment="Весь текст реплики целиком, как есть."), 1.0)
    assert s.bubble(1.0) == ("Весь текст реплики целиком, как есть.", True)


# ── настройки и видимость (PET-6, PET-8) ────────────────────────────────────

def test_settings_drive_visibility_pin_mute_and_hotkey():
    s = PetState()
    assert s.visible and not s.pinned and not s.muted and s.hotkey_input is None
    s.apply("settings", {"pet_visible": False, "pet_pinned": True, "voice_muted": True, "hotkey_input": "Alt+Space"}, 1.0)
    assert not s.visible and s.pinned and s.muted and s.hotkey_input == "Alt+Space"
    s.apply("settings", {"pet_visible": True}, 2.0)
    assert s.visible and not s.pinned
    s.apply("enabled", False, 3.0)                                  # модуль выключен в дашборде: окно скрыто
    assert not s.visible
    s.apply("enabled", True, 4.0)
    assert s.visible


def test_unknown_messages_and_bad_data_are_ignored():
    s = PetState()
    s.apply("странное", {}, 1.0)
    s.apply("presence", "не словарь", 1.0)
    s.apply("settings", None, 1.0)
    assert s.presence == {} and s.settings == {}


# ── очередь потока клиента → окно (PET-3, PET-12) ──────────────────────────────

class RecordingView:
    def __init__(self):
        self.visible = []
        self.frames = []

    def set_visible(self, visible):
        self.visible.append(visible)

    def draw(self, sticker, text, bubble_visible):
        self.frames.append((sticker, text, bubble_visible))


def make_controller(t, **kw):
    q, view = queue.Queue(), RecordingView()
    return PetController(view, q, clock=lambda: t[0], **kw), q, view


def test_controller_turns_queue_messages_into_window_calls():
    t = [100.0]
    seen_settings = []
    c, q, view = make_controller(t, on_settings=seen_settings.append)
    q.put(("connected", True))
    q.put(("settings", {"pet_visible": True, "pet_x": 10}))
    q.put(("presence", pres(emotion="angry", emotion_ts=100.0, speaking=True, segment="Говорю.")))
    assert c.pump() == 3
    assert view.visible == [True] and view.frames == [("angry", "Говорю.", True)]
    assert seen_settings == [{"pet_visible": True, "pet_x": 10}]
    assert c.pump() == 0 and len(view.frames) == 1                   # ничего не изменилось: не перерисовываем
    t[0] += pl.EMOTION_TTL_SECS + 1                                    # эмоция протухла сама, без новых сообщений
    c.pump()
    assert view.frames[-1] == ("idle", "Говорю.", True)
    q.put(("presence", pres(speaking=False, segment="")))
    c.pump()
    t[0] += pl.BUBBLE_HIDE_SECS + 1
    c.pump()
    assert view.frames[-1] == ("idle", "", False)


def test_controller_hides_and_shows_with_pet_visible():
    t = [0.0]
    c, q, view = make_controller(t)
    q.put(("connected", True))
    q.put(("settings", {"pet_visible": False}))
    c.pump()
    assert view.visible == [False]
    q.put(("settings", {"pet_visible": True}))
    c.pump()
    assert view.visible == [False, True]


def test_window_waits_for_settings_but_shows_resting_when_core_never_answers():
    t = [0.0]
    c, q, view = make_controller(t)
    c.pump()
    assert view.visible == [False]                                     # настроек ещё нет: позиция и pet_visible неизвестны
    t[0] += pl.SETTINGS_GRACE_S + 0.1
    c.pump()
    assert view.visible == [False, True] and view.frames[-1][0] == "resting"      # ядра нет: стикер resting


def test_core_loss_turns_the_sticker_into_resting_and_back():
    t = [10.0]
    c, q, view = make_controller(t)
    for m in [("connected", True), ("settings", {}), ("presence", pres(emotion="happy", emotion_ts=10.0))]:
        q.put(m)
    c.pump()
    t[0] += 3.5
    c.pump()
    assert view.frames[-1][0] == "happy"
    q.put(("connected", False))
    c.pump()
    assert view.frames[-1][0] == "resting"
    q.put(("connected", True))
    c.pump()
    assert view.frames[-1][0] == "happy"


def test_bubble_can_be_turned_off_in_settings():
    s = PetState()
    s.apply("settings", {"pet_bubble": False}, 0.0)
    s.apply("presence", pres(speaking=True, segment="Говорю."), 1.0)
    assert s.bubble(1.0) == ("", False)
    s.apply("settings", {"pet_bubble": True}, 2.0)
    assert s.bubble(2.0) == ("Говорю.", True)


def test_bubble_hide_time_follows_the_setting_and_stays_in_limits():
    s = PetState()
    s.apply("presence", pres(speaking=True, segment="Фраза."), 100.0)
    s.apply("presence", pres(speaking=False, segment="Фраза."), 100.0)
    s.apply("settings", {"pet_bubble_secs": 5}, 100.0)
    assert s.bubble(104.0) == ("Фраза.", True) and s.bubble(106.0) == ("", False)
    s.apply("settings", {"pet_bubble_secs": 1}, 100.0)              # меньше 3 с не бывает
    assert s.bubble(102.0) == ("Фраза.", True) and s.bubble(104.0) == ("", False)
    s.apply("settings", {"pet_bubble_secs": 999}, 100.0)            # больше 120 с не бывает
    assert s.bubble(219.0) == ("Фраза.", True) and s.bubble(221.0) == ("", False)
    s.apply("settings", {"pet_bubble_secs": "много"}, 100.0)        # мусор: прежние 25 с
    assert s.bubble(100.0 + pl.BUBBLE_HIDE_SECS - 1) == ("Фраза.", True)
