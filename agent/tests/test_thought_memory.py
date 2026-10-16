"""Мысли как краткосрочная память (перенос по важности) и размышление в свободное время."""
import json

from test_impulses import Clock, Writer, ev, js, service, ScriptedLLM

from adapters.impulse_store import JsonThoughts
from app.idle_reflection import IdleReflector, digest
from app.thought_memory import ThoughtConsolidator
from contracts import ThoughtJournal
from domain import thoughts as dom
from domain.event import KIND_IDLE


def t(id, ts, importance, stage="short", text="мысль", intents=()):
    return {"id": id, "ts": ts, "importance": importance, "stage": stage, "text": text, "intents": list(intents)}


# ── домен ─────────────────────────────────────────────────────────────────────

def test_importance_is_clamped_and_defaults_for_nonsense():
    assert [dom.clean_importance(v) for v in (9, 0, 3.6, "5", None, True)] == [5, 1, 4, 2, 2, 2]


def test_noise_is_not_worth_keeping():
    one = dict(text="м", intents=[{"kind": "say"}], importance=2, status="done")
    assert dom.worth_keeping(**one)
    assert not dom.worth_keeping(**{**one, "status": "limit: tokens"})
    assert not dom.worth_keeping(**{**one, "text": "  "})
    assert not dom.worth_keeping(**{**one, "intents": [{"kind": "none"}]})            # ничего не решено, мелочь
    assert dom.worth_keeping(**{**one, "intents": [{"kind": "none"}], "importance": 4})


def test_plan_promotes_the_important_after_a_while_and_forgets_the_rest():
    now = 100_000.0
    items = [t("fresh-important", now - 60, 5), t("old-important", now - 700, 4), t("old-minor", now - 700, 1),
             t("middling-yesterday", now - 90_000, 3), t("middling-recent", now - 700, 3), t("done", now - 700, 5, stage="long")]
    promote, drop = dom.plan(items, now)
    assert [x["id"] for x in promote] == ["old-important"]
    assert [x["id"] for x in drop] == ["old-minor", "middling-yesterday"]


def test_memory_text_prefers_the_explicit_remember_intent():
    th = t("a", 0, 5, text="думаю", intents=[{"kind": "none"}, {"kind": "remember", "text": "ей нравится чай"}])
    assert dom.memory_text(th) == "ей нравится чай" and dom.memory_text(t("b", 0, 5, text="просто мысль")) == "просто мысль"


# ── размышление пишет в журнал только значимое ───────────────────────────────

async def test_reflection_keeps_importance_and_drops_noise():
    answer = json.dumps({"thought": "она устала", "importance": 4, "intents": [{"kind": "none"}]}, ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer, js(), "бормотание"))
    kept = await svc.reflect(ev())
    assert svc.journal.items == [kept] and kept["importance"] == 4 and kept["stage"] == "short" and kept["text"] == "она устала"
    await svc.reflect(ev())                                    # js(): мысль «м», ничего не решено, важность по умолчанию
    await svc.reflect(ev())                                    # не разобрано
    assert len(svc.journal.items) == 1


async def test_hourly_limit_leaves_no_garbage_in_the_journal():
    svc, _ = service(ScriptedLLM(*[js(intents=[{"kind": "say", "text": "x"}])] * 4), settings=lambda: {"reflection": {"max_per_hour": 1}})
    await svc.reflect(ev())
    skipped = await svc.reflect(ev())
    assert skipped["status"].startswith("skipped") and len(svc.journal.items) == 1


async def test_explicit_remember_goes_straight_to_long_term_memory():
    answer = json.dumps({"thought": "вывод", "importance": 5, "intents": [{"kind": "remember", "text": "любит короткие ответы"}]},
                        ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer))
    th = await svc.reflect(ev())
    assert th["stage"] == "long" and svc.writer.facts == [("любит короткие ответы", "episode")]


async def test_silent_reflection_cannot_speak():
    svc, got = service(ScriptedLLM(js(intents=[{"kind": "say", "text": "привет"}, {"kind": "remember", "text": "факт"}])))
    th = await svc.reflect(ev(KIND_IDLE, "self", data={"silent": True, "untrusted": True}))
    assert got.spoken == [] and th["results"] == ["suppressed: silent reflection", "remembered"]


# ── журнал и перенос ─────────────────────────────────────────────────────────

def test_json_journal_updates_a_thought_and_matches_the_contract(tmp_path):
    j = JsonThoughts(lambda: tmp_path / "t.json")
    assert isinstance(j, ThoughtJournal)
    j.append({"id": "a", "stage": "short"})
    assert j.update("a", stage="long") and j.recent()[0]["stage"] == "long" and not j.update("zzz", stage="x")


async def test_consolidator_moves_important_thoughts_to_memory_once(tmp_path):
    clock = Clock()
    j = JsonThoughts(lambda: tmp_path / "t.json")
    w = Writer()
    for th in (t("imp", clock() - 700, 5, text="важный вывод"), t("minor", clock() - 700, 1), t("fresh", clock(), 5)):
        j.append(th)
    c = ThoughtConsolidator(journal=j, writer=w, clock=clock)
    assert await c.consolidate() == {"promoted": 1, "dropped": 1}
    assert w.facts == [("важный вывод", "episode")]
    assert await c.consolidate() == {"promoted": 0, "dropped": 0}
    assert {x["id"]: x["stage"] for x in j.recent()} == {"imp": "long", "minor": "dropped", "fresh": "short"}


# ── размышление в свободное время ─────────────────────────────────────────────

class FakeReflection:
    def __init__(self):
        self.events = []

    async def reflect(self, event):
        self.events.append(event)
        return {}


class FakeConsolidator:
    def __init__(self):
        self.runs = 0

    async def consolidate(self):
        self.runs += 1
        return {}


def idle(free=True, events=None, **cfg):
    clock = Clock()
    clock.t += 10_000
    refl, cons = FakeReflection(), FakeConsolidator()
    evs = [{"kind": "user", "text": f"реплика {n}"} for n in range(5)] if events is None else events
    r = IdleReflector(reflection=refl, consolidator=cons, is_free=lambda: free, events_since=lambda since, limit: evs[:limit],
                      settings=lambda: {"reflection": {"idle": cfg}}, clock=clock)
    r.last = clock() - 5000
    return r, refl, cons, clock


def test_digest_is_one_line_per_event_and_trimmed():
    out = digest([{"kind": "user", "text": "привет  \n мир"}, {"kind": "say", "text": ""}, {"kind": "game", "text": "я" * 400}], 300)
    assert len(out) <= 300 and "[game]" in out and "[say]" not in out


async def test_idle_reflection_runs_when_free_and_is_silent_and_untrusted():
    r, refl, cons, clock = idle()
    assert await r.tick() == "reflected" and cons.runs == 1
    e = refl.events[0]
    assert e.kind == KIND_IDLE and e.is_self and e.data["silent"] and not e.trusted and "реплика 0" in e.text
    assert await r.tick() == "too soon" and len(refl.events) == 1               # метка сдвинулась: не чаще min_gap_s
    clock.t += 2000
    assert await r.tick() == "reflected"


async def test_idle_reflection_waits_for_free_focus_and_enough_events():
    r, refl, _, _ = idle(free=False)
    assert await r.tick() == "busy" and refl.events == []
    r, refl, _, _ = idle(events=[{"kind": "user", "text": "одна"}])
    assert await r.tick() == "nothing to think about" and refl.events == []
    r, refl, cons, _ = idle(enabled=False)
    assert await r.tick() == "off" and cons.runs == 0
