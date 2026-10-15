"""Трасса задержки и журнал (ТЗ 24): метки по порядку, запись трассы и её сводка, эмоция с интенсивностью, поправки и
ошибки, решения с trace_id, яркость. Модели, голос и сеть подменены: Rig из test_companion_attention."""
import time

import events_log
import fast_brain
import journal
import latency_trace as lt
import salience
import pytest

from test_companion_attention import Rig


@pytest.fixture
def mk(tmp_path, monkeypatch):
    return lambda **kw: Rig(tmp_path, monkeypatch, **kw)


def _traces(**kw):
    events_log.flush()
    return [e for e in events_log.tail(limit=500, kinds={"trace"})]


def _mistakes(kind=None):
    events_log.flush()
    rows = events_log.tail(limit=500, kinds={"mistake"})
    return [r for r in rows if kind is None or (r.get("data") or {}).get("type") == kind]


# ── трасса (TRC-1, TRC-2) ─────────────────────────────────────────────────────────────

def test_marks_are_first_wins_and_keep_stage_order():
    t = lt.start("chat")
    t.mark("llm_done", at=10.0)
    t.mark("llm_done", at=99.0)                 # повторная метка не портит первую
    assert t.marks["llm_done"] == 10.0
    assert "core_in" in t.marks


def test_wire_roundtrip_from_module_keeps_module_marks_and_adds_core_in():
    wire = lt.wire_new("voice", marks={"mic_end": 100.0})
    wire["marks"]["stt_done"] = 130.0
    t = lt.from_bus(wire, "voice.utterance")
    assert t is not None and t.id == wire["id"]
    assert t.marks["mic_end"] == 100.0 and t.marks["stt_done"] == 130.0
    assert "core_in" in t.marks


def test_from_bus_without_trace_is_none():
    assert lt.from_bus(None, "x") is None
    assert lt.from_bus({"marks": {}}, "x") is None


async def test_chat_turn_writes_one_trace_with_stages_in_order(mk):
    rig = mk()
    await rig.say("привет, как дела")
    await rig.pump()
    rows = _traces()
    assert len(rows) == 1
    marks = rows[0]["data"]["marks"]
    order = [s for s in lt.STAGES if s in marks]
    for s in ("core_in", "classify_done", "route", "retrieve_done", "llm_done", "first_sentence_ready"):
        assert s in marks, s
    assert order == sorted(order, key=lt.STAGES.index)
    values = [marks[s] for s in order]
    assert values == sorted(values)               # отсчёт от начала трассы не убывает
    assert rows[0]["data"]["source"] == "dashboard"


async def test_voice_trace_waits_for_audio_marks_then_reports_end_to_sound(mk):
    rig = mk()
    t = lt.from_bus(lt.wire_new("voice", marks={"mic_end": lt.now_ms()}), "voice.utterance")
    await rig.companion.on_voice("привет", {"owner": True, "has_owner": True}, trace=t)
    lt.speak_sent(t)                              # реплику отправили модулю голоса
    await rig.pump()
    assert _traces() == []                        # ждём отметку звука
    lt.audio_arrived({"id": t.id, "marks": {"tts_first_audio": lt.now_ms(), "play_start": lt.now_ms() + 5}})
    rows = _traces()
    assert len(rows) == 1
    d = rows[0]["data"]
    assert d["source"] == "voice"
    assert "play_start" in d["marks"] and "mic_end" in d["marks"]
    assert d["marks"]["mic_end"] == 0.0           # отсчёт от конца фразы
    assert d["end_to_sound_ms"] > 0


def test_summarize_gives_nearest_rank_percentiles_and_steps():
    rows = []
    for k in range(1, 21):                        # шаг route->llm_done = k миллисекунд
        rows.append({"data": {"source": "chat", "marks": {"core_in": 0.0, "route": 1.0, "llm_done": 1.0 + k}}})
    s = lt.summarize(rows)
    assert s["traces"] == 20
    assert s["steps"]["llm_done"]["p50"] == 10.0          # ближний ранг: 10-е из 20
    assert s["steps"]["llm_done"]["p95"] == 19.0
    assert s["at"]["route"]["n"] == 20
    assert s["by_source"] == {"chat": 20}


def test_summarize_ignores_broken_rows():
    assert lt.summarize([{"data": None}, {"data": {"marks": "x"}}, "junk"])["traces"] == 0
    assert lt.percentile([], 0.5) is None


def test_mark_overhead_is_far_below_one_millisecond():
    t = lt.start("chat")
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):
        lt.mark("route", t)
    per_mark_ms = (time.perf_counter() - t0) * 1000 / n
    assert per_mark_ms < 1.0


def test_latency_report_reads_written_traces():
    lt.start("dashboard")
    events_log.log_nowait("trace", "dashboard", trace_id="x", source="dashboard",
                          marks={"core_in": 0.0, "llm_done": 12.5})
    rep = lt.latency_report(3600)
    assert rep["traces"] >= 1 and "steps" in rep


# ── эмоция (TRC-3) ────────────────────────────────────────────────────────────────

def test_emotion_intensity_is_parsed_and_defaults_to_one():
    assert fast_brain._parse('{"emotion": "happy", "action": "none", "intensity": 3}')["intensity"] == 3
    assert fast_brain._parse('{"emotion": "happy", "action": "none"}')["intensity"] == 1
    assert journal.clamp_intensity(9) == 3 and journal.clamp_intensity(-2) == 0
    assert journal.clamp_intensity("слабо") == 1


def test_publish_writes_emotion_with_intensity_and_background_flag():
    fast_brain.publish("chat", {"emotion": "happy", "action": "none", "intensity": 2})
    fast_brain.publish("chat", {"emotion": "idle", "action": "none"})
    events_log.flush()
    rows = events_log.tail(limit=10, kinds={"emotion"})
    fg = [r for r in rows if r["text"] == "happy"][0]["data"]
    bg = [r for r in rows if r["text"] == "idle"][0]["data"]
    assert fg["intensity"] == 2 and fg["bg"] is False and fg["reason"] == "chat"
    assert bg["intensity"] == 1 and bg["bg"] is True
    assert fg["character"]


# ── ошибки (TRC-5) ────────────────────────────────────────────────────────────────

def test_correction_levels_by_pattern():
    assert journal.correction_level("нет, не так") == "yes"
    assert journal.correction_level("Ты ошиблась") == "yes"
    assert journal.correction_level("нет") == "maybe"
    assert journal.correction_level("привет, как дела") == "no"


def test_mistake_rejects_unknown_kind_and_keeps_refs():
    journal.mistake("unknown_kind", "x")
    journal.mistake("denied", "инструмент: нельзя", refs=[1.5, None])
    events_log.flush()
    assert _mistakes("unknown_kind") == []
    rows = _mistakes("denied")
    assert len(rows) == 1 and rows[0]["data"]["refs"] == [1.5]


async def test_correction_phrase_writes_mistake_with_reference(mk):
    rig = mk()
    await rig.say("привет")
    await rig.pump()
    await rig.say("нет, не так")
    await rig.pump()
    rows = _mistakes("correction")
    assert rows and rows[0]["data"]["refs"]


def test_run_ended_colony_died_is_run_lost():
    import core_service
    from types import SimpleNamespace
    hook = core_service.make_run_lost_hook()
    import asyncio
    asyncio.run(hook(SimpleNamespace(text="Колония погибла", data={"outcome": "colony_died"}, created=1.0, trace=None)))
    asyncio.run(hook(SimpleNamespace(text="Забег окончен", data={"outcome": "victory"}, created=2.0, trace=None)))
    events_log.flush()
    assert len(_mistakes("run_lost")) == 1


# ── решения и яркость (TRC-4, TRC-6) ──────────────────────────────────────────────────

def test_decision_carries_trace_id_and_version():
    t = lt.start("voice")
    token = lt.bind(t)
    try:
        journal.decision("chat", "conversation", "привет", confidence=0.8, echelon="gliner")
    finally:
        lt.unbind(token)
    events_log.flush()
    row = events_log.tail(limit=5, kinds={"decision"})[-1]["data"]
    assert row["trace_id"] == t.id
    assert row["rules_version"] == journal.RULES_VERSIONS["conversation"]
    assert row["confidence"] == 0.8 and row["echelon"] == "gliner"


def test_salience_weights_and_reasons_come_from_table():
    value, reasons = salience.score({"explicit_memory": 1.0, "outcome": 1.0})
    assert reasons == ["explicit_memory", "outcome"]
    assert value == 1.0                            # 0.5 + 0.5, не больше 1
    value, reasons = salience.score({})
    assert value == 0.0 and reasons == []
    assert salience.is_outcome("rimworld.run_ended") and not salience.is_outcome("rimworld.colonists")


def test_salience_factors_from_event_fields():
    f = salience.factors_of(urgency=5, delta=-10, memorable=True, kind="rimworld.raid_ended", novelty=True)
    value, reasons = salience.score(f)
    assert set(reasons) == {"importance", "relation", "explicit_memory", "outcome", "novelty"}
    assert value == 1.0


async def test_user_message_carries_salience_and_explicit_memory(mk):
    rig = mk()
    await rig.say("запомни, что я люблю чай")
    events_log.flush()
    rows = [r for r in events_log.tail(limit=20, kinds={"user"}) if "чай" in r["text"]]
    assert rows and rows[-1]["data"]["salience"] > 0
    assert "explicit_memory" in rows[-1]["data"]["reasons"]


async def test_route_decision_is_written_for_each_turn(mk):
    rig = mk()
    await rig.say("привет")
    await rig.pump()
    events_log.flush()
    rows = events_log.tail(limit=50, kinds={"decision"})
    assert any(r["data"].get("route") == "chat" and r["data"].get("trace_id") for r in rows)
