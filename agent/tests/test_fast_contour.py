"""Быстрый контур (ТЗ 25): маршруты и жёсткие правила, стриминг по предложениям, отмена, дедлайн поиска в памяти, запись вне
пути ответа, мгновенный уровень, деградация, бюджеты задержек. Локальная модель, голос и Claude подменены."""
import asyncio
import copy
import threading
import time
from types import SimpleNamespace

import pytest
from test_attention import StubDecider
from test_voice_core import Wired

import events_log
import fast_brain
import latency_trace as lt
import routes
import settings
import speech_out
from app.reply_generator import StreamDeclutter
from attention import Attention
from companion import Companion
from conversation_io import DASHBOARD, UserInput
from relationships import Relationships
from speech_text import SentenceSplitter


# ── заглушки ──────────────────────────────────────────────────────────────────────

class ScriptedLLM:
    """Локальная модель по сценарию. Сценарий потока: строки (куски текста), asyncio.Event (ждать), исключение (поднять)."""

    def __init__(self):
        self.scripts: list[list] = []
        self.default = ["Хорошо, поняла тебя."]
        self.streams: list[dict] = []          # по запросу: {"user": ..., "closed": bool}
        self.quick: list[str] = []             # факты мгновенного уровня, с которыми звали ask
        self.quick_reply = "Беру в работу."
        self.fail_ask: Exception | None = None

    async def ask(self, system, user, *, timeout, max_tokens=40, temperature=0.3, background=False, image_b64=None,
                  parallel=False):
        if self.fail_ask is not None:
            raise self.fail_ask
        if "ТОЛЬКО текстом реплики" not in system:
            return "{}"                          # фоновые вызовы (запоминание и т.п.) не мешают подсчёту мгновенного уровня
        self.quick.append(system)
        return self.quick_reply

    def ask_stream(self, system, user, *, timeout, max_tokens=140, temperature=0.7):
        script = self.scripts.pop(0) if self.scripts else list(self.default)
        rec = {"user": user, "closed": False}
        self.streams.append(rec)
        return self._gen(script, rec)

    @staticmethod
    async def _gen(script, rec):
        try:
            for item in script:
                if isinstance(item, asyncio.Event):
                    await item.wait()
                elif isinstance(item, BaseException):
                    raise item
                else:
                    yield item
                    await asyncio.sleep(0)
        finally:
            rec["closed"] = True


class StreamSpeech:
    """Вывод речи, который принимает предложения потоком (как подключённый голос)."""
    streaming = True

    def __init__(self):
        self.spoken: list[tuple] = []          # (uid, текст, queue)
        self.stopped: list = []

    async def speak(self, utterance_id, text, emotion, priority, *, queue=False):
        self.spoken.append((utterance_id, text, queue))

    async def stop(self, utterance_id=None):
        self.stopped.append(utterance_id)

    def is_speaking(self):
        return False

    def current_segment(self):
        return ""

    @property
    def texts(self):
        return [s[1] for s in self.spoken]


class Router:
    last_kind = "chat"

    def __init__(self, verbs=("найд", "включ")):
        self.verbs, self.handled, self.announces, self.gate = verbs, [], [], None
        self.registry = SimpleNamespace(describe=lambda: "- Дом.")

    def looks_like_action(self, text):
        return any(v in text.lower() for v in self.verbs)

    async def handle(self, text, announce=True):
        self.handled.append(text)
        if self.gate is not None:
            await self.gate.wait()
        self.announces.append(announce() if callable(announce) else announce)
        return ""


async def until(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("не дождались условия")
        await asyncio.sleep(0.005)


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, attention=False, fast=None):
        async def no_reaction(text, rel=None):
            return None
        monkeypatch.setattr(fast_brain, "react_chat", no_reaction)
        monkeypatch.setattr(fast_brain, "publish", lambda *a, **k: None)
        mem = str(tmp_path / "memory")
        self.llm, self.speech = ScriptedLLM(), StreamSpeech()
        self.companion = Companion(mem, Relationships(mem), speech=self.speech, presence=speech_out.PresenceTracker(),
                                   llm=self.llm)
        self.router = self.companion.router = Router()
        self.companion.io = SimpleNamespace(deliver=None)
        settings.update(fast_contour={"stream": True, "merge_rest": False, **(fast or {})})      # потоковый путь выключен по умолчанию: тесты включают его сами
        self.att = None
        if attention:
            self.cfg = copy.deepcopy(settings.DEFAULTS)
            self.att = Attention(self.companion, self.companion.io, settings_source=lambda: self.cfg, decider=StubDecider(None),
                                 rules_file=tmp_path / "rules.json", idle_source=lambda: 0.0, autopump=False)
            self.companion.attention = self.att

    async def say(self, text):
        await self.companion.on_input(UserInput(text, DASHBOARD))

    def dialogue(self):
        return [m["text"] for m in self.companion.memory_reader.recent_dialogue(50) if m["role"] != "user"]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return lambda **kw: Rig(tmp_path, monkeypatch, **kw)


# ── FAST-1, FAST-2: маршруты и жёсткие правила ────────────────────────────────────

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


# ── FAST-4: разбивка и страховка стиля на потоке ───────────────────────────────────

def test_splitter_gives_sentences_as_they_complete():
    sp = SentenceSplitter()
    assert sp.feed("Первое предложение. Вто") == ["Первое предложение."]
    assert sp.feed("рое, длиннее! Третье") == ["Второе, длиннее!"]
    assert sp.feed(" без конца") == [] and sp.flush() == ["Третье без конца"]


def test_splitter_glues_short_bits_and_keeps_decimals():
    sp = SentenceSplitter()
    assert sp.feed("Да. Нет. Конечно, я скажу тебе 3.5 раза. ") == ["Да. Нет.", "Конечно, я скажу тебе 3.5 раза."]


def _stream_declutter(host, sentences):
    d, out = StreamDeclutter(host.reply_generator), []
    for s in sentences:
        out += d.push(s)
    return out + d.finish()


@pytest.mark.parametrize("reply", [
    "Это ответ на вопрос. Вторая мысль здесь. Хочешь, чтобы я рассказала подробнее?",
    "Сейчас опубликую пост. Готово, всё сделано.",
    "Одно предложение, и всё.",
    "Хочешь, чтобы я помогла?",
    "Раз. Два. Три.",
])
def test_stream_declutter_matches_whole_reply_declutter(rig, tmp_path, monkeypatch, reply):
    host = rig().companion
    sents = [x for x in __import__("re").split(r"(?<=[.!?…])\s+", reply) if x]
    assert " ".join(_stream_declutter(host, sents)) == host.reply_generator.declutter(reply)


# ── FAST-4: стриминг в речь ────────────────────────────────────────────────────────

async def test_first_sentence_reaches_tts_before_generation_ends(rig):
    r = rig()
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. Второе предложение ", "тоже здесь. ", gate, "Третье уже после ворот."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    assert not gate.is_set() and not task.done()           # генерация ещё идёт, а первое слово уже ушло в синтез
    assert r.speech.texts == ["Первое предложение тут."]    # второе держится до прихода следующего (_declutter)
    gate.set()
    await task
    assert r.speech.texts == ["Первое предложение тут.", "Второе предложение тоже здесь.", "Третье уже после ворот."]
    assert [s[2] for s in r.speech.spoken] == [False, True, True]       # остальные встают в очередь вывода, не заменяя звучащее
    assert r.dialogue()[-1] == " ".join(r.speech.texts)               # в диалог и журнал одна реплика целиком
    assert r.llm.streams[0]["closed"]


async def test_trace_marks_follow_the_stream(rig):
    r = rig()
    r.llm.scripts = [["Первое предложение тут. Второе предложение здесь."]]
    await r.say("расскажи что-нибудь интересное")
    row = events_log.tail(kinds={"trace"})[-1]["data"]["marks"]
    assert row["llm_first_token"] <= row["first_sentence_ready"] <= row["llm_done"]
    assert lt.ordered_stages(row).index("first_sentence_ready") < lt.ordered_stages(row).index("llm_done")


async def test_stream_stays_off_without_streaming_speech(rig):
    r = rig()
    r.speech.streaming = False
    called = []

    async def ask(system, user, **kw):
        called.append(user)
        return "Целый ответ без потока."
    r.llm.ask = ask
    await r.say("расскажи что-нибудь интересное")
    assert called and not r.llm.streams and r.speech.texts == ["Целый ответ без потока."]


async def test_stream_stays_off_by_setting(rig):
    r = rig(fast={"stream": False})
    r.speech.streaming = True

    async def ask(system, user, **kw):
        return "Ответ целиком."
    r.llm.ask = ask
    await r.say("расскажи что-нибудь интересное")
    assert not r.llm.streams and r.speech.texts == ["Ответ целиком."]


async def test_stream_declutter_drops_trailing_offer(rig):
    r = rig()
    r.llm.scripts = [["Вот ответ на твой вопрос. Ещё одна мысль тут. Хочешь, чтобы я рассказала подробнее?"]]
    await r.say("расскажи что-нибудь интересное")
    assert r.speech.texts == ["Вот ответ на твой вопрос.", "Ещё одна мысль тут."]


async def test_action_marker_is_parsed_before_first_sentence(rig):
    r = rig()
    r.llm.scripts = [["[ДЕЙСТ", "ВИЕ] Сейчас посмотрю, что могу. "]]
    flagged = await r.companion._converse("включи что-нибудь пожалуйста")
    assert flagged is True and r.speech.texts == ["Сейчас посмотрю, что могу."]


async def test_text_that_only_looks_like_marker_is_kept(rig):
    r = rig()
    r.llm.scripts = [["[ДЕЙ", "ствуй так, как считаешь нужным. "]]
    assert await r.companion._converse("что мне делать с этим") is False
    assert "считаешь нужным" in r.speech.texts[0]


# ── FAST-5: отмена ─────────────────────────────────────────────────────────────────

async def test_stop_phrase_cuts_generation_and_speech_mid_stream(rig):
    r = rig(attention=True)
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. Второе предложение здесь. ", gate, "Это после ворот не должно прозвучать. "],
                     ["Хорошо, молчу уже."]]
    first = asyncio.create_task(r.say("расскажи длинную историю"))
    await until(lambda: r.speech.spoken)
    await r.say("Замолчи")
    await asyncio.wait_for(first, 2)
    assert r.llm.streams[0]["closed"]                       # запрос к модели оборван
    assert None in r.speech.stopped                          # звучащее остановлено
    gate.set()
    await asyncio.sleep(0.05)
    assert all("после ворот" not in t for t in r.speech.texts)
    assert r.speech.texts[0] == "Первое предложение тут."
    assert all("после ворот" not in t for t in r.dialogue())   # в память ушло только сказанное


async def test_cancel_reply_aborts_stream_and_queue_of_sentences(rig):
    r = rig()
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. Второе предложение здесь. Третье предложение там. ", gate, "Четвёртое."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: len(r.speech.spoken) >= 2)
    assert r.companion.cancel_reply("barge_in") is True
    await asyncio.wait_for(task, 2)
    gate.set()
    assert r.llm.streams[0]["closed"]
    assert r.speech.texts == ["Первое предложение тут.", "Второе предложение здесь."]   # третье (держалось) не ушло
    assert r.dialogue()[-1] == "Первое предложение тут. Второе предложение здесь."
    assert r.companion.cancel_reply("again") is False


async def test_user_speaking_cuts_reply_in_progress(rig):
    r = rig(attention=True)
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. ", gate, "Второе после ворот."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    r.att.set_user_speaking(True)                            # перебивание: voice.user_speaking
    await asyncio.wait_for(task, 2)
    assert r.llm.streams[0]["closed"] and r.speech.texts == ["Первое предложение тут."]


async def test_emergency_word_cuts_current_reply(rig):
    r = rig(attention=True)
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. ", gate, "Второе после ворот."], ["Что случилось, я здесь."]]
    first = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    await r.say("Помогите, пожар!")
    await asyncio.wait_for(first, 2)
    assert r.llm.streams[0]["closed"] and None in r.speech.stopped
    # второй ответ знает, что рефлекс уже сделан
    assert "Уже сделано" in r.llm.streams[1]["user"] and "экстренное" in r.llm.streams[1]["user"]


# ── FAST-6: параллельность и дедлайн памяти ────────────────────────────────────────

async def test_memory_search_over_deadline_does_not_delay_reply_and_feeds_next_one(rig):
    r = rig(fast={"memory_deadline_ms": 40})
    release = asyncio.Event()
    calls = []

    async def graph_block(query):
        calls.append(query)
        await release.wait()
        return "Факт из графа: любит борщ."
    r.companion._memory_reader.graph_block = graph_block
    t0 = time.monotonic()
    await r.say("что я люблю есть, расскажи")
    assert time.monotonic() - t0 < 1.0 and calls
    assert "любит борщ" not in r.llm.streams[0]["user"]       # ответ ушёл без памяти
    release.set()
    await asyncio.sleep(0.05)                                 # поиск закончился после дедлайна: результат ждёт
    release.clear()
    await r.say("а ещё что-нибудь расскажи пожалуйста")
    assert "любит борщ" in r.llm.streams[1]["user"]           # предзагрузка попала в контекст следующей реплики


async def test_classifier_game_order_and_memory_run_together(rig):
    r = rig(attention=True)
    log = []
    gate = asyncio.Event()

    async def classify(text, questions, timeout=1.0):
        log.append("classify")
        await gate.wait()
        return None

    async def order(text):
        log.append("order")
        return False

    async def graph_block(query):
        log.append("memory")
        return ""
    r.att.ctx.decider.classify = classify
    r.companion.game_order = order
    r.companion._memory_reader.graph_block = graph_block
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: {"classify", "order", "memory"} <= set(log))
    assert not task.done()                                    # классификатор ещё ждёт, а остальное уже работает
    gate.set()
    await asyncio.wait_for(task, 2)


# ── FAST-7: тяжёлое вне пути ───────────────────────────────────────────────────────

async def test_dialogue_write_does_not_stand_on_the_way_of_the_reply(rig):
    r = rig()
    block, entered = threading.Event(), threading.Event()
    orig = r.companion._memory_writer.add_dialogue

    def slow(role, text):
        if role == "user":
            entered.set()
            block.wait(5)
        return orig(role, text)
    r.companion._memory_writer.add_dialogue = slow
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)                      # первое слово звучит, а запись диалога ещё занята
    assert entered.is_set() and not block.is_set()
    block.set()
    await asyncio.wait_for(task, 2)
    assert "расскажи что-нибудь интересное" in [m["text"] for m in r.companion.memory_reader.recent_dialogue(50)]


# ── FAST-3: мгновенный уровень ─────────────────────────────────────────────────────

async def test_slow_task_gets_acknowledgement_formulated_by_model(rig):
    r = rig(fast={"ack_ms": 20, "filler_ms": 0})
    r.router.gate = asyncio.Event()
    task = asyncio.create_task(r.say("Найди рецепт борща"))
    await until(lambda: r.speech.spoken)
    assert r.speech.texts == ["Беру в работу."] and not task.done()    # ушло, пока поручение ещё делается
    assert "поручение" in r.llm.quick[0]                                # модель получила факт, фразы в коде нет
    r.router.gate.set()
    await asyncio.wait_for(task, 2)
    assert r.router.announces == [False]                                # объявление плана не повторяет «принято»


async def test_fast_task_gets_no_acknowledgement(rig):
    r = rig(fast={"ack_ms": 300, "filler_ms": 0})
    await r.say("Найди рецепт борща")
    assert r.speech.spoken == [] and r.llm.quick == [] and r.router.announces == [True]


async def test_slow_chat_answer_gets_filler_then_the_answer(rig):
    r = rig(fast={"ack_ms": 20, "filler_ms": 30})
    gate = asyncio.Event()
    r.llm.quick_reply = "Секунду, думаю."
    r.llm.scripts = [[gate, "Вот и ответ на вопрос."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    assert r.speech.texts == ["Секунду, думаю."]                        # на чате подтверждения нет, только филлер
    gate.set()
    await asyncio.wait_for(task, 2)
    assert r.speech.texts == ["Секунду, думаю.", "Вот и ответ на вопрос."]
    assert len(r.llm.quick) == 1


async def test_no_filler_when_the_answer_is_already_coming(rig):
    r = rig(fast={"ack_ms": 0, "filler_ms": 200})
    await r.say("расскажи что-нибудь интересное")
    await asyncio.sleep(0.25)
    assert r.llm.quick == [] and r.speech.texts == ["Хорошо, поняла тебя."]


# ── FAST-9: деградация ─────────────────────────────────────────────────────────────

async def test_busy_local_model_filler_then_escalation_to_claude(rig):
    r = rig(fast={"ack_ms": 0, "filler_ms": 20})
    gate = asyncio.Event()
    r.llm.scripts = [[gate, RuntimeError("модель занята")]]
    r.llm.quick_reply = "Подожди чуть-чуть."
    asked = []

    async def run_claude(task):
        asked.append(task)
        return "Ответ от Claude."
    r.companion.run_claude = run_claude
    task = asyncio.create_task(r.say("объясни устройство самолёта"))
    await until(lambda: r.speech.spoken)
    assert r.speech.texts == ["Подожди чуть-чуть."] and not asked
    gate.set()
    await asyncio.wait_for(task, 2)
    assert asked == ["объясни устройство самолёта"] and r.speech.texts[-1] == "Ответ от Claude."


async def test_unavailable_model_escalates_without_speaking_partial(rig):
    r = rig(fast={"ack_ms": 0, "filler_ms": 0})
    r.llm.scripts = [[ConnectionError("нет связи")]]
    r.llm.fail_ask = ConnectionError("нет связи")
    asked = []

    async def run_claude(task):
        asked.append(task)
        return "Ответ от Claude."
    r.companion.run_claude = run_claude
    await r.say("объясни устройство самолёта")
    assert asked and r.speech.texts == ["Ответ от Claude."]


async def test_stream_failure_after_some_sentences_does_not_escalate(rig):
    r = rig(fast={"ack_ms": 0, "filler_ms": 0})
    r.llm.scripts = [["Первое предложение тут. Второе предложение здесь. ", ConnectionError("оборвалось")]]
    asked = []

    async def run_claude(task):
        asked.append(task)
        return "Ответ от Claude."
    r.companion.run_claude = run_claude
    await r.say("расскажи что-нибудь интересное")
    assert not asked and r.speech.texts == ["Первое предложение тут.", "Второе предложение здесь."]


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


async def test_each_core_step_on_stubs_fits_the_default_budget(rig):
    """Бюджеты по шагам как тест (FAST-8): на заглушках ядро укладывается в настройку latency_budget по умолчанию."""
    r = rig(attention=True)
    r.llm.scripts = [["Первое предложение тут. Второе предложение здесь."]]
    await r.say("расскажи что-нибудь интересное")
    data = events_log.tail(kinds={"trace"})[-1]["data"]
    limits = settings.DEFAULTS["latency_budget"]
    assert lt.exceeded(data["marks"], data.get("end_to_sound_ms"), limits) == []
    assert not events_log.tail(kinds={"budget_exceeded"})
    assert set(limits) >= {"classify_done", "retrieve_done", "llm_first_token", "first_sentence_ready", "end_to_sound"}


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
    assert speech_out.supports_streaming(StreamSpeech()) is True
    selector = speech_out.SpeechSelector(speech_out.VoiceModuleSpeech(presence), speech_out.TextOnlySpeech(presence))
    assert speech_out.supports_streaming(selector) is False                  # голос не подключён


def test_lm_studio_stream_line_parsing():
    piece = fast_brain._stream_piece
    assert piece('data: {"choices":[{"delta":{"content":"При"}}]}') == "При"
    assert piece('data: {"choices":[{"delta":{}}]}') is None
    assert piece("data: [DONE]") is None and piece(": keep-alive") is None and piece("data: {oops") is None


async def test_compare_stream_first_sentence_beats_whole_reply(tmp_path):
    from tools.measure_e2e import run_compare
    rep = await run_compare(3, str(tmp_path / "cmp"), sentence_ms=60)
    whole = rep["whole"]["at"]["first_sentence_ready"]["p50"]
    stream = rep["stream"]["at"]["first_sentence_ready"]["p50"]
    assert stream < whole - 60                          # поток отдаёт первое предложение примерно на два предложения раньше


def test_reply_tokens_setting_is_limited(rig):
    r = rig(fast={"reply_tokens": 20})
    assert r.companion.reply_generator._reply_tokens() == 40            # ниже 40 не бывает
    settings.update(fast_contour={"reply_tokens": 5000})
    assert r.companion.reply_generator._reply_tokens() == 300
    settings.update(fast_contour={"reply_tokens": "много"})
    assert r.companion.reply_generator._reply_tokens() == 90


async def test_merge_rest_speaks_first_sentence_at_once_and_the_rest_as_one_piece(rig):
    r = rig(fast={"merge_rest": True})
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. Второе предложение ", "тоже здесь. ", gate, "Третье уже после ворот."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    assert not gate.is_set() and not task.done()                  # генерация идёт, первое предложение уже ушло в синтез
    assert r.speech.texts == ["Первое предложение тут."]
    gate.set()
    await task
    assert r.speech.texts == ["Первое предложение тут.", "Второе предложение тоже здесь. Третье уже после ворот."]
    assert [s[2] for s in r.speech.spoken] == [False, True]       # остаток встаёт в очередь вывода одним куском
    assert r.dialogue()[-1] == " ".join(r.speech.texts)


async def test_merge_rest_drops_held_text_when_cancelled(rig):
    r = rig(fast={"merge_rest": True})
    gate = asyncio.Event()
    r.llm.scripts = [["Первое предложение тут. Второе предложение ", "тоже здесь. ", gate, "Третье уже после ворот."]]
    task = asyncio.create_task(r.say("расскажи что-нибудь интересное"))
    await until(lambda: r.speech.spoken)
    r.companion.cancel_reply("stop")
    gate.set()
    await task
    assert r.speech.texts == ["Первое предложение тут."]          # удержанный остаток после «замолчи» не озвучивается
