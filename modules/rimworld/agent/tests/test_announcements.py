"""Правило 14 (объявления о долгих запусках): заметные действия модуля на ПК объявляются фактом, быстрые - нет.

Объявления проверяются через заглушку из conftest (`game_launcher.announced`): протокол, ядро и настоящий ffmpeg не
трогаются. Игру тесты не запускают."""
import pytest

import controller
import game_launcher as gl
import game_watchdog
import run_recorder as rr


class FakeProc:
    """Процесс ffmpeg записи: пока `code` равен None, он работает."""

    def __init__(self):
        self.code = None
        self.stdin = None
        self.stderr = None

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.code = -9


class SilentAudio:
    """Звуковой захват, который не запускается: запись идёт без голоса."""
    ok = False
    stopping = False
    error = "нет звукового устройства"
    parts: list = []
    next_index = 1

    def __init__(self, base, first_index=1):
        self.base = base

    def start(self):
        return False

    def stop(self):
        pass


def _recorder(tmp_path, monkeypatch, ffmpeg="ff"):
    monkeypatch.setattr(rr, "find_ffmpeg", lambda: ffmpeg)
    monkeypatch.setattr(rr.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(rr, "LoopbackRecorder", SilentAudio)
    monkeypatch.setattr(rr.events_log, "log", lambda *a, **k: None)
    return rr.RunRecorder(str(tmp_path), lambda: {"record_runs": True})


def test_self_action_fact_says_the_character_acts_and_goes_through_announce_fact(monkeypatch):
    seen = []
    monkeypatch.setattr(gl, "announce_fact", lambda text, kind="game": seen.append((text, kind)) or text)
    gl.announce_self("запись видео забега: колония «К»", kind="video")
    assert seen == [(gl.SELF_ACTION_PREFIX + "запись видео забега: колония «К»", "video")]
    assert "не пользователь" in seen[0][0] and not seen[0][0].startswith("Запускаю")


def test_recording_start_is_announced_with_its_reason_and_the_sound_state(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path, monkeypatch)
    recorder.start("r1", "Колония", why="забег начался")
    assert recorder.current is not None
    assert len(gl.announced) == 1
    text = gl.announced[0]
    assert text.startswith(gl.SELF_ACTION_PREFIX) and "запускаю запись видео забега" in text
    assert "колония «Колония»" in text and "звука нет" in text and text.endswith("забег начался")


def test_recording_that_does_not_start_is_not_announced(tmp_path, monkeypatch):
    rr.RunRecorder(str(tmp_path), lambda: {"record_runs": False}).start("r0", "К")        # запись выключена
    _recorder(tmp_path, monkeypatch, ffmpeg=None).start("r1", "К")                        # ffmpeg не найден
    assert gl.announced == []


def test_restart_of_a_dead_part_says_why_it_starts_the_next_one(tmp_path, monkeypatch):
    recorder = _recorder(tmp_path, monkeypatch)
    recorder.start("r1", "Колония")
    recorder.current.proc.code = 1                                                       # ffmpeg умер
    old = recorder.ensure_alive()
    assert old is not None and recorder.current is not None
    assert len(gl.announced) == 2
    assert "прежний кусок закрыт" in gl.announced[1] and "пишу следующий кусок" in gl.announced[1]


def test_montage_is_announced_once_and_an_empty_recording_is_not(tmp_path, monkeypatch):
    raw = tmp_path / "run" / "raw_100000.mp4"
    raw.parent.mkdir()
    raw.write_bytes(b"0" * 64)
    monkeypatch.setattr(rr, "MIN_RAW_BYTES", 1)
    monkeypatch.setattr(rr, "find_ffmpeg", lambda: "ff")
    monkeypatch.setattr(rr.events_log, "log", lambda *a, **k: None)
    monkeypatch.setattr(rr.RunRecorder, "_with_audio", lambda self, ffmpeg, rec: None)
    monkeypatch.setattr(rr.RunRecorder, "_run", staticmethod(lambda cmd, out: False))
    recorder = rr.RunRecorder(str(tmp_path), lambda: {})
    empty = rr.Recording("r0", "Пустая", tmp_path / "нет.mp4", 0, duration=500)           # файла нет: монтаж пропущен
    assert recorder.process(empty) is None and gl.announced == []
    rec = rr.Recording("r1", "Колония", raw, 1_000_000.0, duration=600, events=[], out_dir=tmp_path / "out")
    assert recorder.process(rec) is not None
    assert len(gl.announced) == 1
    assert "собираю видео забега" in gl.announced[0] and "Колония" in gl.announced[0]


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    return controller.Controller({"memory_dir": str(tmp_path), "rimworld_api": "http://localhost:8765"})


async def test_quick_start_press_in_the_menu_is_announced(ctrl, monkeypatch):
    monkeypatch.setattr(game_watchdog, "press_quick_start", lambda: True)
    monkeypatch.setattr(controller, "_state_ready", lambda state: True)                  # колония сразу загрузилась
    await ctrl._new_colony()
    assert len(gl.announced) == 1 and gl.announced[0].startswith(gl.SELF_ACTION_PREFIX)
    assert "«Быстрый запуск»" in gl.announced[0]


async def test_menu_navigation_is_announced_once_with_its_goal(ctrl, monkeypatch):
    monkeypatch.setattr(game_watchdog, "press_quick_start", lambda: False)              # кнопки нет: идём через навигацию
    calls = []

    async def fake_navigation(goal):
        calls.append(goal)
        return {"actions": []}
    monkeypatch.setattr(controller, "run_game_navigation", fake_navigation)
    await ctrl._new_colony()
    assert calls == [controller.MENU_GOAL]
    assert len(gl.announced) == 1 and controller.MENU_GOAL[:40] in gl.announced[0]
    assert "веду интерфейс игры" in gl.announced[0]
