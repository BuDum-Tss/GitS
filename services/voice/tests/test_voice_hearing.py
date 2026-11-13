"""Слух системного звука (VOICE-12..14): логика распознавания не менялась; разрешение, подавление своего голоса, сводка."""
import time

import numpy as np

from gits_sdk import host
import hearing as h


def make(cfg, win=None, sensitive=False, on_gist=None):
    return h.Hearing(lambda: cfg, on_gist=on_gist, foreground=lambda: win, is_sensitive=lambda w: sensitive)


async def test_allowed_follows_the_hearing_setting_and_the_window():
    cfg = {"hearing": True}
    assert await make(cfg)._allowed_now()                                           # окна нет: можно
    assert await make(cfg, {"title": "Фильм", "app": "vlc"})._allowed_now()
    assert not await make(cfg, {"title": "Звонок", "app": "Zoom"})._allowed_now()    # звонок
    assert not await make(cfg, {"title": "Банк", "app": "chrome"}, sensitive=True)._allowed_now()
    assert not await make({"hearing": False})._allowed_now()                        # VOICE-13: слух выключен в настройках


async def test_blocked_apps_are_case_insensitive():
    assert not await make({"hearing": True}, {"title": "x", "app": "Discord"})._allowed_now()
    assert not await make({"hearing": True}, {"title": "Telegram Desktop", "app": "x"})._allowed_now()


def test_own_speech_window_is_skipped_unless_muted():
    cfg = {"voice_muted": False}
    ear = make(cfg)
    ear.note_own_speech("Короткая фраза.")
    now = time.time()
    assert ear._overlaps_own_speech(now, now + 1)
    assert not ear._overlaps_own_speech(now + 100, now + 110)
    muted = make({"voice_muted": True})
    muted.note_own_speech("Тихо.")
    assert not muted._overlaps_own_speech(now - 1, now + 5)


def test_recent_text_keeps_the_tail_and_the_window():
    ear = make({})
    now = time.time()
    ear._buf.append((now - 1000, "давно"))
    ear._buf.append((now - 5, "недавно"))
    assert ear.recent_text(180) == "недавно" and "давно" in ear.recent_text(2000)
    ear._buf.append((now, "я" * 2000))
    assert len(ear.recent_text(180, max_chars=900)) == 900


async def test_gist_goes_out_as_a_summary(monkeypatch):
    got = []

    async def on_gist(summary):
        got.append(summary)

    async def fake_ask(system, user, **kw):
        return {"summary": "играет джаз"}
    monkeypatch.setattr(host, "llm_json", fake_ask)
    ear = make({}, on_gist=on_gist)
    now = time.time()
    ear._buf.append((now, "слова " * 30))
    await ear._maybe_gist()
    assert got == ["играет джаз"]
    ear._buf.append((now, "ещё слова " * 30))
    await ear._maybe_gist()                                                          # чаще, чем раз в 3 минуты, не сводит
    assert got == ["играет джаз"]


async def test_short_text_gives_no_gist():
    got = []

    async def on_gist(summary):
        got.append(summary)
    ear = make({}, on_gist=on_gist)
    ear._buf.append((time.time(), "коротко"))
    await ear._maybe_gist()
    assert got == []


async def test_step_skips_quiet_audio_and_records_speech(monkeypatch):
    ear = make({"hearing": True})
    monkeypatch.setattr(ear, "_record", lambda: np.zeros(160, dtype=np.float32))
    monkeypatch.setattr(ear, "_transcribe", lambda audio: (_ for _ in ()).throw(AssertionError("тишину не распознаём")))
    await ear._step()
    assert not ear._buf
    monkeypatch.setattr(ear, "_record", lambda: np.full(160, 0.1, dtype=np.float32))
    monkeypatch.setattr(ear, "_transcribe", lambda audio: "услышанная фраза")
    await ear._step()
    assert [t for _, t in ear._buf] == ["услышанная фраза"]
