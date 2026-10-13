"""Два эшелона фокуса (ТЗ 18, первый - GLiNER, второй - Gemma), общее «не беспокоить» с настройками ядра и профили.

Классификатор и модель не вызываются: подсказки кладутся в элемент внимания как их даёт decide.py, Gemma заменяется
функцией-заглушкой с тем же протоколом, что у fast_brain._ask. Время подменяется (World), sleep нет."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import decide
import fast_brain
import focus
import focus_dispatch as dispatch
from focus_rigs import GAME_POLICIES, MONTAGE_POLICIES, TELEGRAM_POLICIES, World, decide as hook, item, journal_reasons, run
from profile_manager import parse_profile


@pytest.fixture(autouse=True)
def _reset_model_pause():
    dispatch._down_until[0] = 0.0
    yield
    dispatch._down_until[0] = 0.0


def hinted(module, kind, text, priority, *, conf=None, hint=None, hint_conf=None, focus_needed=None, focus_conf=None,
           category=None, urgency=None) -> SimpleNamespace:
    """Элемент внимания с подсказками GLiNER так, как их кладёт attention._apply_classification."""
    it = item(module, kind, text, priority, urgency=urgency, conf=conf, category=category)
    extra = {"policy_hint": hint, "policy_conf": hint_conf, "focus_needed": focus_needed, "focus_conf": focus_conf}
    it.data.update({k: v for k, v in extra.items() if v is not None})
    return it


def world() -> World:
    w = World(now=1_000_000.0)
    w.add("rimworld", GAME_POLICIES)
    w.add("montage", MONTAGE_POLICIES)
    w.add("telegram", TELEGRAM_POLICIES, active="available")
    return w


def llm_records(mgr):
    return [r for r in mgr._journal if r["type"] == "llm"]


# ── разбор ответа модели (строгий JSON) ──────────────────────────────────────────────────────

def test_parse_strict_accepts_one_object_and_code_fence_only():
    assert dispatch.parse_strict('{"reason": "x", "operations": []}') == {"reason": "x", "operations": []}
    assert dispatch.parse_strict('```json\n{"operations": []}\n```') == {"operations": []}
    for bad in ("просто текст", '["список"]', '{"operations": [}', ""):
        with pytest.raises(ValueError):
            dispatch.parse_strict(bad)


def test_parse_urgency_only_integers_0_to_5():
    assert dispatch.parse_urgency('{"urgency": 4}') == 4
    assert dispatch.parse_urgency('{"urgency": true}') is None
    assert dispatch.parse_urgency('{"urgency": 9}') is None
    assert dispatch.parse_urgency("не json") is None


def test_enabled_switch_by_env(monkeypatch):
    monkeypatch.delenv("GLADOS_FOCUS_LLM", raising=False)
    assert dispatch.enabled()
    monkeypatch.setenv("GLADOS_FOCUS_LLM", "0")
    assert not dispatch.enabled()


# ── первый эшелон: GLiNER решает сам, когда уверен ───────────────────────────────────────────

async def test_clear_classifier_answer_decides_without_llm():
    w = world()
    mgr = w.manager()                                              # w.llm_calls фиксирует любой вызов модели
    hook(mgr, hinted("montage", "montage.render_done", "Рендер готов", 80.0, conf=0.95, hint="montage/working",
                     hint_conf=0.9, focus_needed=True, focus_conf=0.9, category="news"))
    await run(mgr, w)
    assert w.llm_calls == []
    assert mgr.slot is not None and mgr.slot.module == "montage"
    assert mgr.slot.echelon == "gliner"
    switch = next(r for r in mgr._journal if r["type"] == "switch")
    assert switch["echelon"] == "gliner" and switch["confidence"] == 0.95 and switch["hint"] == "montage/working"


async def test_classifier_says_focus_not_needed_defers_without_llm():
    w = world()
    mgr = w.manager()
    hook(mgr, hinted("telegram", "telegram.message", "Скидка 70%", 70.0, conf=0.9, focus_needed=False, focus_conf=0.9,
                     category="spam"))
    await run(mgr, w)
    assert w.llm_calls == [] and mgr.slot is None
    assert any("не требует фокуса" in r for r in journal_reasons(mgr, "defer"))
    assert next(r for r in mgr._journal if r["type"] == "defer")["echelon"] == "gliner"


async def test_critical_event_is_rules_echelon():
    w = world()
    mgr = w.manager()
    hook(mgr, hinted("rimworld", "rimworld.raid", "Набег", 90.0, conf=0.95, category="danger", urgency=5))
    await run(mgr, w)
    assert mgr.slot is not None and next(r for r in mgr._journal if r["type"] == "switch")["echelon"] == "rules"


# ── второй эшелон: Gemma только для неоднозначного ───────────────────────────────────────────

async def test_low_confidence_asks_gemma_and_applies_strict_answer():
    w = world()
    mgr = w.manager(llm=False)
    seen = []

    async def ask(system, user, **kw):
        seen.append((system, json.loads(user), kw))
        return json.dumps({"reason": "подождать", "operations": [
            {"type": "reprioritize_event", "event_id": json.loads(user)["event"]["id"], "priority": 50}]})

    mgr.llm = dispatch.make_llm(ask)
    hook(mgr, hinted("montage", "montage.render_done", "Рендер", 70.0, conf=0.4, category="news"))
    await run(mgr, w)
    system, ctx, kw = seen[0]
    assert "reprioritize_event" in system and kw["timeout"] == dispatch.CALL_TIMEOUT_S and kw["background"] is True
    assert ctx["event"]["confidence"] == 0.4
    ev = next(iter(mgr.events.values()))
    assert ev.priority == 50 and ev.echelon == "gemma"      # шаг 20, не больше 25
    rec = llm_records(mgr)[0]
    assert rec["status"] == "ok" and rec["echelon"] == "gemma" and "низкая уверенность" in rec["why"]


async def test_conflicting_hint_asks_gemma():
    w = world()
    mgr = w.manager(llm=False)
    mgr.llm = dispatch.make_llm(_fixed_text('{"reason": "нет", "operations": []}'))
    hook(mgr, hinted("telegram", "telegram.message", "Монтаж готов?", 70.0, conf=0.9, hint="montage/working",
                     hint_conf=0.9, category="question"))
    await run(mgr, w)
    assert llm_records(mgr) and "конфликт подсказки" in llm_records(mgr)[0]["why"]


async def test_two_claimants_with_close_priority_ask_gemma():
    w = world()
    mgr = w.manager(llm=False)
    mgr.llm = dispatch.make_llm(_fixed_text('{"reason": "", "operations": []}'))
    hook(mgr, hinted("rimworld", "rimworld.x", "A", 80.0, conf=0.9, category="news"))
    hook(mgr, hinted("montage", "montage.y", "B", 85.0, conf=0.9, category="news"))
    await run(mgr, w)
    assert any("два претендента" in r["why"] for r in llm_records(mgr))


async def test_confident_clear_event_near_threshold_is_not_asked():
    """У порога, но классификатор уверен и подсказка совпадает с модулем: решают правила, модель не спрашиваем."""
    w = world()
    mgr = w.manager(llm=False)
    mgr.llm = dispatch.make_llm(_fixed_text('{"reason": "", "operations": []}'))
    hook(mgr, hinted("montage", "montage.render_done", "Рендер", 62.0, conf=0.95, hint="montage/working", hint_conf=0.9,
                     category="news"))
    await run(mgr, w)
    assert llm_records(mgr) == []


async def test_gemma_timeout_is_unavailable_and_rules_go_on(monkeypatch):
    monkeypatch.setattr(focus, "LLM_TIMEOUT_S", 0.05)
    w = world()
    mgr = w.manager(llm=False)

    async def slow(system, user, **kw):
        await asyncio.sleep(5)
        return "{}"

    mgr.llm = dispatch.make_llm(slow)
    hook(mgr, hinted("montage", "montage.render_done", "Рендер", 80.0, conf=0.4, category="news"))
    await run(mgr, w)
    rec = llm_records(mgr)[0]
    assert rec["status"] == "unavailable" and "тайм-аут" in rec["reason"]
    assert mgr.slot is not None and mgr.slot.module == "montage"      # правила сработали без модели


async def test_gemma_answer_not_json_is_invalid_and_changes_nothing():
    w = world()
    mgr = w.manager(llm=False)
    mgr.llm = dispatch.make_llm(_fixed_text("вот мой ответ: переключить"))
    hook(mgr, hinted("montage", "montage.render_done", "Рендер", 80.0, conf=0.4, category="news"))
    await run(mgr, w)
    assert llm_records(mgr)[0]["status"] == "invalid"
    assert all(e.priority == 80.0 for e in mgr.events.values())


async def test_model_failure_puts_gemma_on_short_pause():
    calls = []

    async def failing(system, user, **kw):
        calls.append(1)
        raise ConnectionError("LM Studio выключена")

    call = dispatch.make_llm(failing)
    with pytest.raises(ConnectionError):
        await call({"event": {}})
    with pytest.raises(RuntimeError, match="пауза"):
        await call({"event": {}})
    assert len(calls) == 1                                           # второй раз модель не звали


async def test_recheck_by_gemma_sets_urgency_and_echelon():
    w = world()
    mgr = w.manager(llm=False)
    mgr.recheck = dispatch.make_recheck(_fixed_text('{"urgency": 5}'))
    hook(mgr, hinted("rimworld", "rimworld.attack", "Нападение у ворот", 50.0, conf=0.3, category="danger"))
    await run(mgr, w)
    ev = next(iter(mgr.events.values()))
    assert ev.critical and mgr.slot is not None and mgr.slot.module == "rimworld"
    assert mgr.slot.echelon == "gemma"


def _fixed_text(text):
    async def ask(system, user, **kw):
        return text
    return ask


# ── «не беспокоить»: одно состояние с настройками ядра (FOC-6) ────────────────────────────────

async def test_dnd_all_is_the_shared_setting_and_exceptions_survive():
    w = world()
    state = {"dnd": False}
    mgr = w.manager(dnd_all_of=lambda: state["dnd"], set_dnd_all=lambda on: state.__setitem__("dnd", on))

    await mgr.set_dnd("all", True)
    assert state["dnd"] is True and mgr.dnd_all is True
    hook(mgr, hinted("telegram", "telegram.message", "Привет", 70.0, category="personal_message"))
    await run(mgr, w)
    assert mgr.slot is None and any("не беспокоить" in r for r in journal_reasons(mgr, "defer"))

    await mgr.allow_once("telegram")                                 # разовое исключение проходит режим...
    hook(mgr, hinted("telegram", "telegram.question", "Срочно?", 70.0, category="question"))
    await run(mgr, w)
    assert mgr.slot is not None and mgr.slot.module == "telegram"
    assert state["dnd"] is True                                      # ...но сам режим не отменён


async def test_dnd_changed_in_settings_is_seen_by_focus_at_once():
    w = world()
    state = {"dnd": True}
    mgr = w.manager(dnd_all_of=lambda: state["dnd"], set_dnd_all=lambda on: state.__setitem__("dnd", on))
    hook(mgr, hinted("telegram", "telegram.message", "Привет", 70.0, category="personal_message"))
    await run(mgr, w)
    assert mgr.slot is None
    state["dnd"] = False                                             # выключили в дашборде или голосом, не через фокус
    await run(mgr, w)
    assert mgr.slot is not None and mgr.slot.module == "telegram"


async def test_dnd_all_without_shared_setting_is_local_and_persisted(tmp_path):
    w = world()
    mgr = w.manager(tmp_path, state=True)
    await mgr.set_dnd("all", True)
    assert mgr._state_dict()["dnd_all"] is True


# ── профили: модули, которым профиль не даёт переключать фокус ───────────────────────────────

async def test_profile_quiet_module_defers_but_is_not_saved_as_user_setting(tmp_path):
    w = world()
    mgr = w.manager(tmp_path, state=True, profile_quiet_of=lambda: ("telegram",))
    hook(mgr, hinted("telegram", "telegram.message", "Привет", 70.0, category="personal_message"))
    await run(mgr, w)
    assert mgr.slot is None and any("профиль" in r for r in journal_reasons(mgr, "defer"))
    assert mgr.snapshot()["dnd"]["profile"] == ["telegram"]
    assert "telegram" not in mgr._state_dict()["dnd_modules"]


# ── подсказки политики для GLiNER (первый эшелон) ────────────────────────────────────────────

def test_policy_options_are_active_policies_of_connected_modules():
    w = world()
    w.disconnect("montage")
    mgr = w.manager(modules_of=lambda: list(w.modules) + ["montage"])
    options = mgr.policy_options()
    assert set(options) == {"rimworld/observe", "rimworld/combat", "telegram/replying"}
    assert all(isinstance(v, str) and v for v in options.values())


# ── вопросы и разбор для классификатора (decide.py) ──────────────────────────────────────────

def test_event_questions_add_policy_only_with_options():
    assert decide.event_questions() is decide.EVENT_QUESTIONS
    assert "policy" not in decide.EVENT_QUESTIONS and "focus_needed" in decide.EVENT_QUESTIONS
    q = decide.event_questions({"montage/working": "монтаж видео"})
    assert q["policy"]["options"]["montage/working"] == "монтаж видео"
    assert decide.NONE_POLICY in q["policy"]["options"]
    assert "policy" not in decide.EVENT_QUESTIONS                   # базовый словарь не меняется


def test_parse_event_reads_focus_needed_and_policy_above_threshold():
    answers = {"focus_needed": {"answer": "no", "confidence": 0.9},
               "policy": {"answer": "none", "confidence": 0.7},
               "category": {"answer": "news", "confidence": 0.8}}
    cls = decide.parse_event(answers, 0.35)
    assert cls.focus_needed is False and cls.focus_conf == 0.9
    assert cls.policy == "none" and cls.policy_conf == 0.7 and cls.category == "news"
    low = decide.parse_event({"focus_needed": {"answer": "yes", "confidence": 0.2}}, 0.35)
    assert low.focus_needed is None


# ── профиль: поле focus.quiet_modules ────────────────────────────────────────────────────────

def test_profile_focus_quiet_modules_parse():
    assert parse_profile({"id": "x", "title": "X", "focus": {"quiet_modules": ["telegram"]}}).focus_quiet == ("telegram",)
    assert parse_profile({"id": "y", "title": "Y"}).focus_quiet == ()
    with pytest.raises(ValueError):
        parse_profile({"id": "z", "title": "Z", "focus": {"quiet_modules": "telegram"}})


async def test_preempted_call_is_not_a_model_failure():
    """Срочный ответ пользователю вытесняет фоновый вызов: модель жива, пауза после этого не нужна."""
    async def preempted(system, user, **kw):
        raise fast_brain.Preempted("срочный")

    with pytest.raises(fast_brain.Preempted):
        await dispatch.make_llm(preempted)({"event": {}})

    async def ok(system, user, **kw):
        return '{"operations": []}'

    assert await dispatch.make_llm(ok)({"event": {}}) == {"operations": []}
