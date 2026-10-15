"""Лимит Claude: распознавание, время сброса и запоминание для всех процессов."""
import time

import claude_quota as q
import events_log


def test_recognises_limit_text_only():
    assert q.looks_like_limit("You've hit your session limit · resets 7:40pm (Europe/Moscow)")
    assert not q.looks_like_limit("Обычная реплика ГЛаДОС")
    assert not q.looks_like_limit("x" * 500 + " session limit")          # длинный текст это не сообщение об ошибке


def test_reset_time_is_in_the_future():
    t = q._parse_reset("resets 7:40pm (Europe/Moscow)")
    assert t > time.time() and t - time.time() <= 86400 + 120
    assert q._parse_reset("без времени") is None


def test_note_blocks_until_reset(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path))
    events_log.init(str(tmp_path))
    assert not q.blocked()
    assert q.note("You've hit your session limit · resets 11:59pm") is True
    assert q.blocked() and q.blocked_until() > time.time()
    assert q.note("просто текст") is False
