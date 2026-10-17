"""Разговор с менеджером внимания (ТЗ 03, ATT-13, ATT-18, ATT-28): речь идёт через очередь, «замолчи» до модели, намерения
классификатора, ответ в канал модуля мимо очереди; без классификатора и без менеджера поведение прежнее (приёмка ТЗ 02, п. 2).
Локальная модель, питомец и сеть подменены."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from hub_helpers import FakeIO, FakeSpeech
from test_attention import StubDecider, ans

import approvals
import fast_brain
import settings
import speech_out
from attention import Attention
from companion import Companion
from conversation_io import DASHBOARD, VOICE, Channel, UserInput, current_channel
from relationships import Relationships


class Registry:
    def describe(self):
        return "- Дом: умеет включать свет."


class StubRouter:
    def __init__(self, verbs=("найд", "включ")):
        self.verbs, self.handled, self.last_kind = verbs, [], "chat"
        self.registry = Registry()

    def looks_like_action(self, text):
        return any(v in text.lower() for v in self.verbs)

    async def handle(self, text, announce=True):
        self.handled.append(text)
        return ""                                       # простое действие выполнено, сказать нечего


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, attention=True, decider_answers=None):
        self.llm_calls: list[tuple[str, str]] = []
        self.order: list[str] = []
        self.reply = "Ответ из модели."

        async def ask(system, user, timeout=30, max_tokens=40, image_b64=None, temperature=0.3, background=False):
            self.llm_calls.append((system, user))
            self.order.append("llm")
            return self.reply
        monkeypatch.setattr(fast_brain, "_ask", ask)

        async def no_reaction(text, rel=None):
            return None
        monkeypatch.setattr(fast_brain, "react_chat", no_reaction)
        monkeypatch.setattr(fast_brain, "publish", lambda *a, **k: None)

        mem = str(tmp_path / "memory")
        self.speech = FakeSpeech()
        presence = speech_out.PresenceTracker()
        self.companion = Companion(mem, Relationships(mem), speech=self.speech, presence=presence)
        self.companion.router = StubRouter()
        self.io = FakeIO()
        self.delivered: list[tuple[Channel, str]] = []

        async def deliver(channel, text):
            self.delivered.append((channel, text))
            return True
        self.io.deliver = deliver
        self.companion.io = self.io
        self.cfg = copy.deepcopy(settings.DEFAULTS)
        self.decider = StubDecider(decider_answers)
        self.att = None
        if attention:
            self.att = Attention(self.companion, self.io, settings_source=lambda: self.cfg, decider=self.decider,
                                 rules_file=tmp_path / "rules.json", idle_source=lambda: 0.0, autopump=False)
            self.companion.attention = self.att
        self.graph_queries: list[str] = []
        self.companion.graph = SimpleNamespace(
            enqueue=lambda *a, **k: None, status_text=lambda: "граф",
            context_block=self._graph_block)
        orig_stop = self.speech.stop

        async def stop(uid=None):
            self.order.append("stop")
            await orig_stop(uid)
        self.speech.stop = stop

    async def _graph_block(self, query):
        self.graph_queries.append(query)
        return ""

    async def say(self, text, channel=DASHBOARD):
        await self.companion.on_input(UserInput(text, channel))

    async def pump(self):
        await self.att.speech.pump()
        await self.att.settle()


def intent(name, conf=0.9, memory="yes"):
    return {"intent": ans(name, conf), "needs_memory": ans(memory, 0.9)}


@pytest.fixture
def mk(tmp_path, monkeypatch):
    return lambda **kw: Rig(tmp_path, monkeypatch, **kw)


async def test_reply_goes_to_speech_queue_as_conversation_with_priority_95(mk):
    r = mk()
    await r.say("Привет")
    assert r.speech.spoken == []                                      # сама companion.say в вывод речи не пишет
    cand = r.att.speech.items[0]
    assert (cand.source, cand.priority, cand.mode, cand.text, cand.channel) == ("conversation", 95, "verbatim", "Ответ из модели.", DASHBOARD)
    await r.pump()
    assert [s[1] for s in r.speech.spoken] == ["Ответ из модели."] and r.speech.spoken[0][3] == 95


async def test_without_attention_she_speaks_at_once_as_before(mk):
    r = mk(attention=False)
    await r.say("Привет")
    assert [s[1] for s in r.speech.spoken] == ["Ответ из модели."]
    assert r.companion.presence.snapshot()["busy"] is False           # занятость, как раньше, ведёт on_input


async def test_busy_and_focus_come_from_the_focus_stack_with_attention(mk):
    r = mk()
    await r.say("Привет")
    p = r.companion.presence.snapshot()
    assert p["busy"] is True and p["focus"] == "conversation: Привет"


async def test_stop_phrase_cuts_speech_before_any_model_call(mk):
    r = mk()
    r.speech.speaking = True
    r.att.speech.new(source="attention:a1", mode="fact", text="ждёт", priority=60)
    await r.say("Замолчи")
    assert r.order[0] == "stop" and "llm" in r.order                  # остановка раньше любого вызова модели
    assert r.speech.stopped[0] is None
    assert [c.text for c in r.att.speech.items] == ["Ответ из модели."]     # старая очередь снята, осталась только новая реплика


async def test_reply_to_stop_is_not_removed_and_speaks(mk):
    r = mk()
    await r.say("Тихо")
    await r.pump()
    assert [s[1] for s in r.speech.spoken] == ["Ответ из модели."]


async def test_reply_to_module_channel_bypasses_the_queue(mk):
    r = mk()
    channel = Channel("module", "telegram", "tg:1")
    await r.say("Привет из телеги", channel)
    assert r.att.speech.items == [] and r.speech.spoken == []
    assert r.delivered == [(channel, "Ответ из модели.")]             # доставка модулю, вслух не звучит


async def test_speak_about_goes_through_queue_and_planner_formulates_it(mk):
    r = mk()
    r.reply = json.dumps({"say": True, "text": "Колония горит."})
    token = current_channel.set(VOICE)
    try:
        await r.companion._speak_about("Начался пожар.", "что там?", must=True)
    finally:
        current_channel.reset(token)
    assert r.llm_calls == []                                          # модель пока не звали: факт ждёт очереди
    cand = r.att.speech.items[0]
    assert (cand.source, cand.mode, cand.must, cand.user_text, cand.priority) == ("conversation", "fact", True, "что там?", 95)
    await r.pump()
    assert len(r.llm_calls) == 1 and "Начался пожар." in r.llm_calls[0][1]
    assert [s[1] for s in r.speech.spoken] == ["Колония горит."]
    assert r.att.speech.items == []                                   # реплика сформулирована и сказана ровно один раз


async def test_proactive_line_is_a_low_priority_candidate_with_its_own_expiry(mk):
    r = mk()
    await r.companion.say("Давно молчим.", "sarcastic", source="proactive")
    cand = r.att.speech.items[0]
    assert (cand.source, cand.priority, cand.emotion) == ("proactive", 30, "sarcastic")


async def test_question_announcement_is_spoken_before_the_wait_starts(mk):
    r = mk()
    asked = asyncio.create_task(approvals.request("Хочу удалить файл.", r.companion.say))
    for _ in range(50):
        if r.att.speech.items:
            break
        await asyncio.sleep(0)
    cand = r.att.speech.items[0]
    assert cand.source == "question" and "Разрешаешь?" in cand.text
    assert approvals.is_announcing()
    await r.pump()
    for _ in range(50):
        if not approvals.is_announcing():
            break
        await asyncio.sleep(0)
    assert approvals.has_pending() and not approvals.is_announcing()
    assert await approvals.try_answer("да") and await asked is True


# ── классификатор обращений ATT-28 ────────────────────────────────────────────────────────────────

async def test_about_self_intent_answers_with_the_capability_list(mk):
    r = mk(decider_answers=intent("about_self", 0.53))
    await r.say("Что ты умеешь делать?")                              # в тексте есть «сдела»: без классификатора ушло бы в планировщик
    assert r.companion.router.handled == []
    system, user = r.llm_calls[0]
    assert "Дом: умеет включать свет." in user and "возможностях" in user


async def test_action_intent_goes_to_router_even_without_verbs(mk):
    r = mk(decider_answers=intent("action", 0.8))
    await r.say("Погода на завтра в Казани")
    assert r.companion.router.handled == ["Погода на завтра в Казани"]


async def test_attention_rule_intent_goes_to_router(mk):
    r = mk(decider_answers=intent("attention_rule", 0.7))
    await r.say("Час не трогай меня сообщениями из телеграма")
    assert r.companion.router.handled == ["Час не трогай меня сообщениями из телеграма"]


async def test_chat_intent_without_verbs_skips_the_router(mk):
    r = mk(decider_answers=intent("chat", 0.9))
    await r.say("Расскажи анекдот")
    assert r.companion.router.handled == [] and len(r.llm_calls) == 1


async def test_chat_intent_with_action_verb_still_goes_to_router(mk):
    r = mk(decider_answers=intent("chat", 0.9))
    await r.say("Найди рецепт борща")                                  # looks_like_action сильнее
    assert r.companion.router.handled == ["Найди рецепт борща"]


async def test_low_confidence_intent_is_ignored(mk):
    r = mk(decider_answers=intent("action", 0.48))
    await r.say("Погода на завтра")
    assert r.companion.router.handled == []


async def test_unavailable_classifier_keeps_the_old_behaviour(mk):
    r = mk(decider_answers=None)
    await r.say("Найди рецепт борща")
    await r.say("Привет")
    assert r.companion.router.handled == ["Найди рецепт борща"]
    assert len(r.llm_calls) == 1 and r.graph_queries == ["Привет"]    # память, как раньше, искалась


async def test_needs_memory_no_does_not_skip_graph_search_until_classifier_is_tuned(mk):
    # needs_memory смещён в «нет» (замеры ТЗ 03): память по нему не отключается
    r = mk(decider_answers=intent("chat", 0.9, memory="no"))
    await r.say("Что я говорил вчера про отпуск?")
    assert r.graph_queries == ["Что я говорил вчера про отпуск?"]


async def test_stop_intent_skips_graph_search(mk):
    r = mk(decider_answers=intent("stop", 0.9, memory="no"))
    await r.say("Замолчи")
    assert r.graph_queries == []


async def test_approval_answer_still_wins_over_everything(mk):
    r = mk(decider_answers=intent("approval", 0.9))
    asked = asyncio.create_task(approvals.ask("approval", "Можно?", timeout_s=30))
    for _ in range(50):
        if approvals.has_pending():
            break
        await asyncio.sleep(0)
    await r.say("Да")
    assert await asked == (True, "user") and r.llm_calls == [] and r.companion.router.handled == []


async def test_game_order_still_precedes_routing(mk):
    r = mk(decider_answers=intent("action", 0.9))
    taken = []

    async def order(text):
        taken.append(text)
        return True
    r.companion.game_order = order
    await r.say("Построй стену")
    assert taken == ["Построй стену"] and r.companion.router.handled == []
    await asyncio.sleep(0)                                            # «принимаю» ставится фоновой задачей
    cand = r.att.speech.items[0]
    assert cand.mode == "fact" and cand.must and "срочное указание" in cand.text    # ответ «принято» тоже идёт через очередь
