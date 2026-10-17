"""Хвосты ТЗ 30: размышление после разговора и эпизода (IMP-10), планы пользователя через импульсы (PlanNudger)."""
import json

from test_impulses import Clock, Journal, Mem, ScriptedLLM, Sink, Writer, js
from test_impulses import ev as make_event

from types import SimpleNamespace

from adapters.impulse_store import JsonReminders
from app.impulse_timer import ImpulseTimer
from app.plan_nudge import PlanNudger
from app.reflection import ReflectionService
from domain import rules
from domain.event import KIND_AFTER_CONVERSATION

NOW = 100_000.0


def test_after_conversation_event_goes_to_think():
    assert rules.think_route(origin="self", kind=KIND_AFTER_CONVERSATION, decision="D0", urgency=1, priority=0.0,
                             memorable=False, needs_reply=False)
    assert not rules.think_route(origin="external", kind=KIND_AFTER_CONVERSATION, decision="D3", urgency=3, priority=0.0,
                                 memorable=False, needs_reply=False)


def _service(llm):
    return ReflectionService(llm=llm, memory=Mem(), writer=Writer(), journal=Journal(),
                             prompt=lambda: {"system": "S", "format": "F", "after": "ПОДВЕДИ ИТОГ"}, clock=Clock())


async def test_conversation_and_episode_reflection_adds_the_summing_up_prompt():
    for kind, origin in ((KIND_AFTER_CONVERSATION, "self"), ("rimworld.episode.summary", "external")):
        llm = ScriptedLLM(js(intents=[{"kind": "remember", "text": "короткие ответы ей нравятся"}]))
        svc = _service(llm)
        thought = await svc.reflect(make_event(kind=kind, origin=origin, text="итог"))
        assert "ПОДВЕДИ ИТОГ" in llm.calls[0][0]
        assert thought["results"] == ["remembered"] and svc.writer.facts == [("короткие ответы ей нравятся", "episode")]


async def test_other_events_do_not_get_the_summing_up_prompt():
    llm = ScriptedLLM(js(intents=[{"kind": "none"}]))
    await _service(llm).reflect(make_event())
    assert "ПОДВЕДИ ИТОГ" not in llm.calls[0][0]


def test_real_prompt_has_the_after_section():
    import prompts
    assert prompts.reflection()["after"]


# ── планы пользователя ────────────────────────────────────────────────────────

class PlanTasks:
    """Планы-задачи: напоминание помечается (`mark_nudged`) и не повторяется."""

    def __init__(self, plans):
        self.plans = [SimpleNamespace(meta={}, **p) for p in plans]

    async def due_for_nudge(self, lead_s=0.0, kinds=()):
        return list(self.plans)

    async def mark_nudged(self, task_id):
        for p in self.plans:
            if p.id == task_id:
                p.meta["nudged"] = NOW


async def test_plans_become_untrusted_reminders_before_the_due_time(tmp_path):
    clock, sink = Clock(), Sink()
    store = JsonReminders(lambda: tmp_path / "r.json", clock)
    timer = ImpulseTimer(store, sink, clock=clock)
    tasks = PlanTasks([{"id": "p1", "goal": "сдать отчёт", "due": NOW + 3600}, {"id": "p2", "goal": "позвонить маме", "due": NOW}])
    nudger = PlanNudger(tasks, timer.add, clock=clock, lead_s=1800)
    assert await nudger.sync() == 2 and await nudger.sync() == 0       # второй просмотр ничего нового
    pending = {r["text"]: r for r in store.pending()}
    assert pending["У пользователя скоро запланировано: сдать отчёт"]["due"] == NOW + 1800
    assert pending["У пользователя скоро запланировано: позвонить маме"]["due"] == NOW      # срок уже пришёл: сразу
    assert all(r["untrusted"] and r["source"]["kind"] == "plan" for r in pending.values())
    clock.t = NOW + 1
    assert await timer.fire_due() == 1                                  # пришло одно событие, остальное ждёт срока
    assert json.dumps(sink.events[0], ensure_ascii=False).count("позвонить маме") == 1
    assert sink.events[0]["kind"] == "self.reminder" and sink.events[0]["data"]["untrusted"] is True


async def test_conversation_going_quiet_notifies_the_hook_once(tmp_path):
    from test_companion_split import make
    c, _ = make(tmp_path)
    got = []
    c.on_conversation_end = got.append
    c._memory_writer.add_dialogue("user", "расскажи про Луну")
    c._memory_writer.add_dialogue("assistant", "Это спутник Земли.")
    c._pending_memory = 1
    c._idle_consolidate()
    assert got == []                                        # одной реплики мало для итога
    c._pending_memory = 2
    c._idle_consolidate()
    assert len(got) == 1 and "расскажи про Луну" in got[0] and "Это спутник Земли." in got[0]


def test_dashboard_page_shows_thoughts_and_latency_budget():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "static" / "ui" / "index.html").read_text(encoding="utf-8")
    for needle in ('data-tab="thoughts"', 'id="tab-thoughts"', "/api/thoughts", 'id="latency-budget"', "L.budget"):
        assert needle in html, needle
