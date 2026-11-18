"""Наблюдатель простоя колонистов, событие rimworld.idle_colonists и его проводка в controller (без игры и сети)."""
import asyncio
import importlib
import sys
from pathlib import Path

import pytest

import controller
import core_link
import events_log
import module_manifest
import statediff
from idle_watch import IDLE_GRACE_S, IDLE_REPEAT_S, IdleWatch


def pawn(pid, job="Wait_Wander", name=None, **kw):
    return {"id": pid, "name": name or f"К{pid}", "current_job": job, "mood_pct": 0.6, "health_pct": 1.0, **kw}


def snap(pawns, paused=False, threats=()):
    return statediff.snapshot({"tick": 1, "paused": paused, "save_id": "s", "threats": list(threats),
                               "colony": {"pawns": list(pawns)}})


def test_below_grace_gives_nothing_then_one_event_with_everyone_idle():
    w = IdleWatch()
    s = snap([pawn("a"), pawn("b", "Wait"), pawn("c", "Research")])
    assert w.update(s, 0) is None
    assert w.update(s, IDLE_GRACE_S - 1) is None
    ev = w.update(s, IDLE_GRACE_S + 60)
    assert [c["id"] for c in ev["colonists"]] == ["a", "b"]
    assert ev["colonists"][0] == {"id": "a", "name": "Кa", "job": "Wait_Wander", "idle_s": IDLE_GRACE_S + 60}
    assert "Кa (бродит, 2 мин)" in ev["text"] and "Кb (ждёт, 2 мин)" in ev["text"] and ev["text"].endswith("дела у них нет.")


def test_repeat_is_rate_limited_and_new_idler_fires_at_once():
    w = IdleWatch()
    s = snap([pawn("a")])
    w.update(s, 0)
    assert w.update(s, 50) is not None
    assert w.update(s, 50 + IDLE_REPEAT_S - 1) is None
    assert w.update(s, 50 + IDLE_REPEAT_S) is not None            # прошло достаточно: напоминаем
    t = 200
    s2 = snap([pawn("a"), pawn("b")])
    assert w.update(s2, t) is None                                  # b только начал простаивать
    ev = w.update(s2, t + IDLE_GRACE_S)                             # b дошёл до порога: сразу, не ждём повтора
    assert [c["id"] for c in ev["colonists"]] == ["a", "b"]


@pytest.mark.parametrize("kw", [{"paused": True}, {"threats": [{"id": "t", "name": "П", "kind": "man"}]}])
def test_pause_and_threats_are_not_counted(kw):
    w = IdleWatch()
    w.update(snap([pawn("a")]), 0)
    assert w.update(snap([pawn("a")], **kw), 100) is None
    assert w.update(snap([pawn("a")]), 101) is None                 # отсчёт после паузы или боя начался заново
    assert w.update(snap([pawn("a")]), 101 + IDLE_GRACE_S) is not None


def test_fight_job_skips_the_tick():
    w = IdleWatch()
    w.update(snap([pawn("a")]), 0)
    assert w.update(snap([pawn("a"), pawn("b", "AttackMelee")]), 100) is None


@pytest.mark.parametrize("kw", [{"is_drafted": True}, {"is_downed": True}, {"is_prisoner": True}])
def test_drafted_downed_prisoner_are_not_idle(kw):
    w = IdleWatch()
    s = snap([pawn("a", **kw)])
    w.update(s, 0)
    assert w.update(s, 500) is None


def test_everyone_busy_resets_the_count():
    w = IdleWatch()
    idle, busy = snap([pawn("a")]), snap([pawn("a", "Research")])
    w.update(idle, 0)
    assert w.update(idle, 100) is not None
    assert w.update(busy, 101) is None
    assert w.update(idle, 102) is None                              # заново с нуля
    assert w.update(idle, 102 + IDLE_GRACE_S) is not None


def test_event_is_declared_with_priority_50_ttl_120_and_replace_and_is_valid():
    assert core_link.EVENTS["idle_colonists"] == ("rimworld.idle_colonists", 3)
    spec = {k["kind"]: k for k in module_manifest.event_kinds()}["rimworld.idle_colonists"]
    assert spec["default_priority"] == 50 and spec["ttl_s"] == 120 and spec["coalesce"] and spec["replace"]
    assert "critical" not in spec and "passive" not in spec
    assert any(e["kind"] == "rimworld.idle_colonists" for e in module_manifest.EMITS)
    root = str(Path(__file__).resolve().parents[4])
    sys.path.insert(0, root)
    try:
        kinds = importlib.import_module("agent.services.attention_v2.kinds")
    except Exception:
        pytest.skip("модуль внимания ядра недоступен")
    finally:
        sys.path.remove(root)
    ks = kinds.KindSpec.from_dict(spec)
    assert ks.replace and ks.coalesce and ks.ttl_s == 120


def test_module_settings_declared_and_work_interval():
    assert module_manifest.SETTINGS_SCHEMA["idle_grace_s"]["min"] == 10 and module_manifest.SETTINGS_SCHEMA["idle_grace_s"]["max"] == 600
    assert module_manifest.SETTINGS_SCHEMA["auto_speed"]["default"] == "max"
    assert module_manifest.WORKSPACE["work_interval_s"] == 90


# ── controller ────────────────────────────────────────────────────────────────

class FakeLink:
    def __init__(self):
        self.sent = []
        self.module_settings = {}

    async def event(self, kind, data=None, text=None, urgency=None):
        self.sent.append((kind, data, text))


class Pol:
    def __init__(self, focus):
        self.in_focus = focus


def make(focus=True, link=True):
    c = controller.Controller.__new__(controller.Controller)
    c.link = FakeLink() if link else None
    c.policies = Pol(focus)
    c._idle = IdleWatch()
    return c


async def drive(c, monkeypatch, times, s):
    it = iter(times)
    monkeypatch.setattr(controller.time, "time", lambda: next(it))
    monkeypatch.setattr(events_log, "log", lambda *a, **k: None)
    for _ in times:
        c._watch_idle(s)
    await asyncio.sleep(0)


async def test_controller_sends_only_in_focus_and_with_link(monkeypatch):
    s = snap([pawn("a")])
    c = make()
    await drive(c, monkeypatch, [0, 100], s)
    assert [(k, d["colonists"][0]["id"]) for k, d, _t in c.link.sent] == [("idle_colonists", "a")]
    assert c.link.sent[0][2].startswith("Простаивают колонисты: Кa")
    c = make(focus=False)
    await drive(c, monkeypatch, [0, 100], s)
    assert c.link.sent == []
    c = make(link=False)
    await drive(c, monkeypatch, [0, 100], s)           # без связи не падает
