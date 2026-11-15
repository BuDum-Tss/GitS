"""Размышление не ведёт отдельного журнала: значимая мысль пишется событием `thought` в журнал событий, лимиты считаются в памяти процесса."""
import json

from test_impulses import Clock, ev, js, service, ScriptedLLM

from services.reflection import reflection as dom
from kernel.event import KIND_AFTER_CONVERSATION


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


# ── мысль это событие журнала ─────────────────────────────────────────────────

async def test_significant_thought_is_logged_as_a_thought_event_with_source_and_intents():
    answer = json.dumps({"thought": "она устала", "importance": 4, "intents": [{"kind": "say", "text": "отдохни"}]}, ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer))
    kept = await svc.reflect(ev("telegram.message", "external"))
    assert svc.log.items == [{"kind": "thought", "text": "она устала", "importance": 4, "intents": ["say"], "module": "echo",
                              "event_kind": "telegram.message"}]
    assert kept["text"] == "она устала" and "stage" not in kept and "id" not in kept


async def test_empty_unparsed_and_undecided_thoughts_are_not_logged():
    svc, _ = service(ScriptedLLM(js(), "бормотание"))
    await svc.reflect(ev())                                    # js(): мысль «м», ничего не решено, важность по умолчанию
    await svc.reflect(ev())                                    # не разобрано
    assert svc.log.items == []


async def test_hourly_limit_works_without_any_journal_and_leaves_no_events():
    svc, _ = service(ScriptedLLM(*[js(intents=[{"kind": "say", "text": "x"}])] * 4), settings=lambda: {"reflection": {"max_per_hour": 1}})
    await svc.reflect(ev())
    skipped = await svc.reflect(ev())
    assert skipped["status"].startswith("skipped") and len(svc.log.items) == 1


async def test_hourly_limit_releases_after_an_hour():
    clock = Clock()
    svc, _ = service(ScriptedLLM(*[js(intents=[{"kind": "say", "text": "x"}])] * 4), clock=clock,
                     settings=lambda: {"reflection": {"max_per_hour": 1}})
    await svc.reflect(ev())
    assert (await svc.reflect(ev()))["status"].startswith("skipped")
    clock.t += 3601
    assert (await svc.reflect(ev()))["status"] == "done"


async def test_chain_depth_limit_still_refuses_reminders():
    svc, got = service(ScriptedLLM(js(intents=[{"kind": "remind", "text": "ещё раз", "in_s": 60}])),
                       settings=lambda: {"reflection": {"max_depth": 1}})
    th = await svc.reflect(ev(data={"depth": 1}))
    assert th["results"] == ["refused: chain depth"] and got.reminded == []


async def test_explicit_remember_writes_a_note_through_the_writer():
    answer = json.dumps({"thought": "вывод", "importance": 5, "intents": [{"kind": "remember", "text": "любит короткие ответы"}]},
                        ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer))
    th = await svc.reflect(ev())
    assert th["results"] == ["remembered"] and svc.writer.facts == [("любит короткие ответы", "episode")]


async def test_reflection_never_writes_its_conclusions_into_memory_by_itself():
    answer = json.dumps({"thought": "Это напоминание о цели, которое я уже знаю", "importance": 5, "intents": [{"kind": "none"}]},
                        ensure_ascii=False)
    svc, _ = service(ScriptedLLM(answer, answer))
    await svc.reflect(ev())
    await svc.reflect(ev(KIND_AFTER_CONVERSATION, "self", data={"untrusted": True}))
    assert svc.writer.facts == [] and not hasattr(svc, "consolidate")


async def test_silent_reflection_cannot_speak():
    svc, got = service(ScriptedLLM(js(intents=[{"kind": "say", "text": "привет"}, {"kind": "remember", "text": "факт"}])))
    th = await svc.reflect(ev(KIND_AFTER_CONVERSATION, "self", data={"silent": True, "untrusted": True}))
    assert got.spoken == [] and th["results"] == ["suppressed: silent reflection", "remembered"]
