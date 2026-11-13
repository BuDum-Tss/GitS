"""Мысли как краткосрочная память: оценка важности, журнал и перенос в долговременную память (ReflectionService.consolidate)."""
import json

from test_impulses import Clock, ev, js, service, ScriptedLLM

from adapters.impulse_store import JsonThoughts
from contracts import ThoughtJournal
from domain import thoughts as dom
from domain.event import KIND_AFTER_CONVERSATION


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
    th = await svc.reflect(ev(KIND_AFTER_CONVERSATION, "self", data={"silent": True, "untrusted": True}))
    assert got.spoken == [] and th["results"] == ["suppressed: silent reflection", "remembered"]


# ── журнал и перенос ─────────────────────────────────────────────────────────

def test_json_journal_updates_a_thought_and_matches_the_contract(tmp_path):
    j = JsonThoughts(lambda: tmp_path / "t.json")
    assert isinstance(j, ThoughtJournal)
    j.append({"id": "a", "stage": "short"})
    assert j.update("a", stage="long") and j.recent()[0]["stage"] == "long" and not j.update("zzz", stage="x")


async def test_consolidate_moves_important_thoughts_to_memory_once():
    clock = Clock()
    svc, _ = service(ScriptedLLM(), clock=clock)
    for th in (t("imp", clock() - 700, 5, text="важный вывод"), t("minor", clock() - 700, 1), t("fresh", clock(), 5)):
        svc.journal.append(th)
    assert await svc.consolidate() == {"promoted": 1, "dropped": 1}
    assert svc.writer.facts == [("важный вывод", "episode")]
    assert await svc.consolidate() == {"promoted": 0, "dropped": 0}
    assert {x["id"]: x["stage"] for x in svc.journal.recent()} == {"imp": "long", "minor": "dropped", "fresh": "short"}


async def test_reflection_consolidates_by_itself_but_not_more_often_than_the_gap():
    clock = Clock()
    answer = json.dumps({"thought": "вывод", "importance": 5, "intents": [{"kind": "none"}]}, ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer, answer, answer), clock=clock)
    svc.journal.append(t("old", clock() - 700, 5, text="давний вывод"))
    await svc.reflect(ev())
    assert svc.writer.facts == []                                              # с прошлого переноса прошло меньше паузы
    clock.t += 601
    await svc.reflect(ev())
    assert ("давний вывод", "episode") in svc.writer.facts
