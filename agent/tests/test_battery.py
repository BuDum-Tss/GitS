"""Батарейка Claude: заряд по окну, сон без заряда, расход без чтения кэша, времена восстановления."""
import tempfile
import time

import claude_battery
import claude_quota
import settings


def test_battery_level_and_sleep(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    settings.update(claude_token_budget=1_000_000)
    assert claude_battery.status()["level"] == 1.0
    claude_battery.record(250_000, "t")
    st = claude_battery.status()
    assert st["used"] == 250_000 and st["level"] == 0.75 and not st["sleeping"]
    claude_battery.record(800_000, "t")
    assert claude_battery.status()["sleeping"]
    assert claude_battery.status(now=time.time() + claude_battery.WINDOW_S + 5)["level"] == 1.0   # окно освободилось


def test_battery_tokens_skip_cache_reads_and_limit_message_empties_it(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    assert claude_battery.tokens_of({"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 10**6}) == 12
    assert claude_quota.note("You've hit your session limit · resets 7:40pm")
    st = claude_battery.status()
    assert st["level"] == 0.0 and st["sleeping"] and st["blocked"]


def test_battery_recovery_times(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("MEMORY_DIR", d)
    settings.init(d)
    settings.update(claude_token_budget=1_000_000)
    assert claude_battery.status()["full_at"] is None
    t0 = time.time()
    claude_battery.record(600_000, "a")
    claude_battery.record(500_000, "b")
    st = claude_battery.status()
    assert st["sleeping"] and st["next_release_tokens"] == 600_000
    assert abs(st["wake_at"] - (t0 + claude_battery.WINDOW_S)) < 5       # после выхода первой порции спать уже не нужно
    assert st["full_at"] >= st["next_release_at"]
