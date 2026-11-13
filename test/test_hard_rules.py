"""Жёсткие правила и маршруты (ТЗ 25): «замолчи», экстренные слова, ответ на вопрос о разрешении, таблица решений,
разбивка на предложения, бюджеты задержек, контракт speak модуля голоса."""
from types import SimpleNamespace

import pytest
from test_voice_core import Wired

import events_log
import latency_trace as lt
import routes
import speech_out
from speech_text import SentenceSplitter


@pytest.mark.parametrize("text", ["Замолчи!", "замол", "Пожалуйста, стоп", "заткнись", "хватит болтать", "ЗАМОЛЧИТЕ"])
def test_hard_rule_stop_works_even_on_cut_recognition(text):
    m = routes.hard_rules().match(text)
    assert m is not None and m.kind == "stop"


@pytest.mark.parametrize("text", ["хватит ли денег", "помоги мне найти файл", "стоп-кадр из фильма", "привет", ""])
def test_hard_rule_does_not_fire_on_ordinary_speech(text):
    assert routes.hard_rules().match(text) is None


def test_hard_rule_emergency_and_approval_only_with_open_question():
    rules = routes.hard_rules()
    assert rules.match("Помогите, пожар!").kind == "emergency"
    assert rules.match("да") is None
    m = rules.match("Да.", pending_question=True)
    assert m.kind == "approval" and m.answer == "yes"
    assert rules.match("не надо", pending_question=True).answer == "no"
    assert rules.match("да, и ещё расскажи про погоду в Москве", pending_question=True) is None


def test_hard_rules_come_from_yaml_without_code_changes(tmp_path):
    f = tmp_path / "rules.yaml"
    f.write_text("version: 7\nstop:\n  patterns: ['^тишина$']\n  stems: []\n", encoding="utf-8")
    custom = routes.HardRules.load(f)
    assert custom.version == 7 and custom.match("Тишина!").kind == "stop" and custom.match("замолчи") is None


def test_decisions_map_to_routes_by_table():
    assert routes.of_decision("D0") == ["ignore"] and routes.of_decision("D1") == ["ignore"]
    assert routes.of_decision("D3") == ["chat"] and routes.of_decision("D4") == ["reflex", "chat"]
    assert routes.of_decision("R2") == ["reflex"] and routes.of_decision("D2", think=True) == ["chat", "think"]
    assert set(routes.DECISION_ROUTES) == {"D0", "D1", "D2", "D3", "D4", "R1", "R2", "R3", "R4"}


def test_input_routes_reflex_with_already_done_then_chat():
    hard = routes.hard_rules().match("замолчи")
    d = routes.of_input(intent="chat", looks_action=False, hard=hard)
    assert d.routes == ["reflex", "chat"] and d.already_done and d.rule == "stop" and d.echelon == "hard"
    assert routes.of_input(intent="chat", looks_action=True).routes == ["task"]
    assert routes.of_input(intent="action", looks_action=False).routes == ["task"]
    assert routes.of_input(intent=None, looks_action=False, order_taken=True).routes == ["task"]
    approval = routes.hard_rules().match("да", pending_question=True)
    assert routes.of_input(intent=None, looks_action=False, hard=approval).routes == ["reflex"]


def test_splitter_gives_sentences_as_they_complete():
    sp = SentenceSplitter()
    assert sp.feed("Первое предложение. Вто") == ["Первое предложение."]
    assert sp.feed("рое, длиннее! Третье") == ["Второе, длиннее!"]
    assert sp.feed(" без конца") == [] and sp.flush() == ["Третье без конца"]


def test_splitter_glues_short_bits_and_keeps_decimals():
    sp = SentenceSplitter()
    assert sp.feed("Да. Нет. Конечно, я скажу тебе 3.5 раза. ") == ["Да. Нет.", "Конечно, я скажу тебе 3.5 раза."]


# ── FAST-8: бюджеты ────────────────────────────────────────────────────────────────

@pytest.fixture
def clock(monkeypatch):
    t = {"ms": 1000.0}
    monkeypatch.setattr(lt, "now_ms", lambda: t["ms"])
    yield t
    lt.set_budget_source(None)


def _turn(clock, steps, sound=None):
    """Трасса с управляемыми часами: steps - [(этап, сколько мс прошло)]."""
    t = lt.start("dashboard")
    for stage, dt in steps:
        clock["ms"] += dt
        t.mark(stage)
    if sound is not None:
        lt.speak_sent(t)
    lt.finish_core(t)
    if sound is not None:
        clock["ms"] += sound
        lt.audio_arrived({"id": t.id, "marks": {"tts_first_audio": clock["ms"] - 10, "play_start": clock["ms"]}})
    return t


async def test_budget_exceeded_is_journaled_and_visible_in_report(clock):
    lt.set_budget_source(lambda: {"classify_done": 100, "retrieve_done": 400, "end_to_sound": 800})
    _turn(clock, [("classify_done", 250), ("route", 10), ("retrieve_done", 50), ("llm_first_token", 900)], sound=200)
    rows = events_log.tail(kinds={"budget_exceeded", "trace"})
    over = [r for r in rows if r["kind"] == "budget_exceeded"][0]["data"]["stages"]
    assert {o["stage"] for o in over} == {"classify_done", "end_to_sound"}
    assert [o for o in over if o["stage"] == "classify_done"][0]["ms"] == 250
    report = lt.summarize([r for r in rows if r["kind"] == "trace"])
    assert report["budget"] == {"traces_over": 1, "by_stage": {"classify_done": 1, "end_to_sound": 1}}


def test_within_budget_writes_no_event(clock):
    lt.set_budget_source(lambda: {"classify_done": 100, "end_to_sound": 800})
    _turn(clock, [("classify_done", 40), ("route", 10)], sound=300)
    assert not events_log.tail(kinds={"budget_exceeded"})
    assert lt.summarize(events_log.tail(kinds={"trace"}))["budget"]["traces_over"] == 0


def test_streaming_first_sentence_step_is_measured_in_time_order(clock):
    lt.set_budget_source(lambda: {"first_sentence_ready": 300, "llm_done": 5000})
    # первое предложение готово раньше конца ответа модели: шаг считается по времени, а не по порядку в STAGES
    _turn(clock, [("llm_first_token", 100), ("first_sentence_ready", 120), ("llm_done", 2000)])
    assert not events_log.tail(kinds={"budget_exceeded"})
    steps = lt.summarize(events_log.tail(kinds={"trace"}))["steps"]
    assert steps["first_sentence_ready"]["p50"] == 120 and steps["llm_done"]["p50"] == 2000


# ── контракт speak и потоковая строка LM Studio ─────────────────────────────────────

async def test_streamed_sentences_queue_in_voice_module_instead_of_replacing(tmp_path):
    async with Wired(tmp_path) as w:
        mod = await w.module()
        await w.speech.speak("u-00000001", "Первое предложение.", None, 50)
        await w.speech.speak("u-00000002", "Второе предложение.", None, 50, queue=True)
        await w.speech.speak("u-00000003", "Новая реплика.", None, 50)
        await w.wait(lambda: len(mod.spoken) == 3)
        assert [s["replace"] for s in mod.spoken] == [False, False, True]
        assert w.speech.supports_streaming() and w.speech.live_count() == 3
        await w.stop_module(mod)


def test_supports_streaming_follows_the_output():
    presence = speech_out.PresenceTracker()
    assert speech_out.supports_streaming(speech_out.TextOnlySpeech(presence)) is False
    assert speech_out.supports_streaming(SimpleNamespace(spoken=[])) is False
    assert speech_out.supports_streaming(SimpleNamespace(spoken=[], streaming=True)) is True
    selector = speech_out.SpeechSelector(speech_out.VoiceModuleSpeech(presence), speech_out.TextOnlySpeech(presence))
    assert speech_out.supports_streaming(selector) is False                  # голос не подключён
