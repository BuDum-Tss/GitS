"""Менеджер внимания (ТЗ 03, ATT-6…ATT-12, ATT-19…ATT-30): правила, формула, решения, склейка, классификатор, сводка,
вопросы. Классификатор подменён заглушкой, часы фиктивные, фоновых задач нет (`autopump=False`), sleep не используется."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from hub_helpers import FakeCompanion, FakeIO

import approvals
import events_log
import settings
from attention import Attention, Question
from conversation_io import Channel
from decide import EVENT_QUESTIONS
from environments.base import ToolError


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def ans(value, conf=0.9):
    return {"answer": value, "confidence": conf, "probabilities": {value: conf}}


class StubDecider:
    """Классификатор с заданными ответами: answers - словарь ответов или функция (текст -> ответы)."""

    def __init__(self, answers=None):
        self.answers = answers
        self.calls: list[tuple[str, list]] = []
        self.last_ms = 11.0
        self.gate: asyncio.Event | None = None

    async def classify(self, text, questions, timeout=1.0):
        self.calls.append((text, list(questions)))
        if self.gate is not None:
            await self.gate.wait()
        return self.answers(text) if callable(self.answers) else self.answers


class FakeRel:
    def __init__(self, people=None):
        self.people = people or {}

    def get(self, key):
        return self.people.get(key)


class FakeProfiles:
    def __init__(self, weights=None):
        self.weights = weights or {}

    def current(self):
        return SimpleNamespace(attention={"weights": self.weights})


class Rig:
    def __init__(self, tmp_path, decider=None, weights=None, people=None):
        self.clock = Clock()
        self.companion = FakeCompanion()
        self.companion._rel = FakeRel(people)
        self.io = FakeIO()
        self.cfg = copy.deepcopy(settings.DEFAULTS)
        self.idle = 0.0
        self.decider = decider or StubDecider(None)
        self.att = Attention(self.companion, self.io, clock=self.clock, decider=self.decider,
                             settings_source=lambda: self.cfg, rules_file=tmp_path / "rules.json",
                             idle_source=lambda: self.idle, autopump=False)
        self.att.profiles = FakeProfiles(weights)
        self.modules = FakeModules()
        self.att.modules = self.modules

    async def submit(self, module, **event):
        event.setdefault("kind", f"{module}.ping")
        event.setdefault("text", "Событие")
        await self.att.submit(module, event)
        await self.att.settle()
        return self.att.last_decision

    async def play(self, *, advance=100.0):
        """Запустить планировщик речи один раз и дождаться реплики; время сдвигается, чтобы пауза между репликами не мешала."""
        self.clock.t += advance
        cand = await self.att.speech.pump()
        await self.att.settle()
        return cand

    def candidates(self):
        return self.att.speech.items


class FakeModules:
    def __init__(self):
        self.records = {}

    def declare(self, module_id, title=None, emits=(), enabled=True):
        manifest = {"emits": [{"kind": k, "description": "x", "default_urgency": u} for k, u in emits]}
        self.records[module_id] = SimpleNamespace(title=title or module_id, manifest=manifest)

    def get(self, module_id):
        return self.records.get(module_id)

    def is_enabled(self, module_id):
        return True


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def evt_log(kind="attention"):
    return events_log.tail(50, {kind})


# ── формула ATT-9: каждый множитель отдельно ──────────────────────────────────────────────────────

def score_of(rig, kind="rimworld.raid", urgency=3, sender=None, addressed=None, age=0.0, module=None):
    item = rig.att._make_item(module or kind.split(".")[0], {"kind": kind, "text": "т", "sender": sender,
                                                              "addressed_to_me": addressed}, rig.clock.t - age)
    item.urgency = urgency
    return rig.att._score(item, rig.cfg, rig.att._att(rig.cfg))


def test_base_formula_is_urgency_times_twenty(rig):
    s = score_of(rig)
    assert s["priority"] == 60 and (s["w_profile"], s["w_sender"], s["w_rule"], s["w_user"], s["freshness"]) == (1, 1, 1, 1, 1)
    assert score_of(rig, urgency=0)["priority"] == 0 and score_of(rig, urgency=5)["priority"] == 100


def test_priority_is_clamped_to_100(rig):
    rig.att.profiles = FakeProfiles({"rimworld.*": 3.0})
    assert score_of(rig, urgency=5)["priority"] == 100


def test_profile_weight_takes_the_longest_matching_pattern(tmp_path):
    rig = Rig(tmp_path, weights={"*": 0.5, "rimworld.*": 1.5, "rimworld.raid": 2.0, "telegram.group": 0.4})
    assert score_of(rig, "rimworld.raid")["w_profile"] == 2.0
    assert score_of(rig, "rimworld.letter")["w_profile"] == 1.5
    assert score_of(rig, "telegram.group")["w_profile"] == 0.4
    assert score_of(rig, "desktop.window")["w_profile"] == 0.5
    assert score_of(Rig(tmp_path), "rimworld.raid")["w_profile"] == 1.0           # профиль без весов


def test_sender_weight(tmp_path):
    rig = Rig(tmp_path, people={"tg:1": {"affinity": 60.0}, "tg:2": {"affinity": 59.9}, "Вася": {"affinity": -10}})
    w = lambda sender: score_of(rig, "telegram.dm", sender=sender)["w_sender"]       # noqa: E731
    assert w({"id": "tg:0", "is_owner": True}) == 1.5
    assert w({"id": "tg:1", "name": "Маша"}) == 1.3                  # близость 0.6 и выше
    assert w({"id": "tg:2"}) == 1.0                                  # известный человек
    assert w({"id": "tg:9", "name": "Вася"}) == 1.0                  # известен по имени
    assert w({"id": "tg:9", "name": "Незнакомец"}) == 0.8
    assert w(None) == 1.0


def test_rule_weight_boost_only_and_unmatched_events(rig):
    rig.att.rules.add(module="telegram", kind=None, sender="мама", effect="only", minutes=30)
    rig.att.rules.add(module=None, kind="desktop.*", sender=None, effect="boost", minutes=30)
    w = lambda kind, name=None: score_of(rig, kind, sender={"id": "x", "name": name} if name else None)["w_rule"]   # noqa: E731
    assert w("telegram.dm", "Мама") == 1.5
    assert w("telegram.dm", "Аня") == 0.2 and w("telegram.group") == 0.2
    assert w("rimworld.raid") == 1.0                                  # only с модулем не трогает другие модули
    assert w("desktop.window") == 2.0
    rig.att.rules.add(module=None, kind=None, sender="мама", effect="only", minutes=30)
    assert w("rimworld.raid") == 0.2                                  # only без модуля: несовпавшие события всех модулей


def test_user_weight_dnd_quiet_and_urgent_exception(rig):
    rig.cfg["dnd"] = True
    assert score_of(rig)["w_user"] == 0.3
    assert score_of(rig, urgency=5, addressed=True)["w_user"] == 1.0   # срочное обращение к ней звучит
    assert score_of(rig, urgency=5, addressed=False)["w_user"] == 0.3
    assert score_of(rig, urgency=4, addressed=True)["w_user"] == 0.3
    rig.cfg["dnd"], rig.cfg["quiet_until"] = False, rig.clock.t + 60
    assert score_of(rig)["w_user"] == 0.3
    rig.clock.t += 61
    assert score_of(rig)["w_user"] == 1.0                              # таймер «не отвлекать» истёк


def test_away_user_keeps_weight_but_marks_state(rig):
    rig.idle = 601
    s = score_of(rig)
    assert s["w_user"] == 1.0 and s["away"] is True
    rig.idle = 600
    assert score_of(rig)["away"] is False


def test_freshness_halves_every_half_life_by_kind(rig):
    assert score_of(rig, "rimworld.raid", age=60)["freshness"] == pytest.approx(0.5)       # rimworld.*: 60 с
    assert score_of(rig, "telegram.dm", age=900)["freshness"] == pytest.approx(0.5)        # telegram.*: 900 с
    assert score_of(rig, "desktop.window", age=240)["freshness"] == pytest.approx(0.25)    # desktop.*: 120 с
    assert score_of(rig, "montage.job", age=600)["freshness"] == pytest.approx(0.5)        # *.job: 600 с
    assert score_of(rig, "echo.ping", age=300)["freshness"] == pytest.approx(0.5)          # *: 300 с
    assert score_of(rig, "rimworld.raid", age=60)["priority"] == pytest.approx(30)


def test_half_life_is_configurable(rig):
    rig.cfg["attention"]["half_life"] = {"*": 10}
    assert score_of(rig, "telegram.dm", age=10)["freshness"] == pytest.approx(0.5)


def test_broken_attention_settings_fall_back_to_defaults(rig):
    rig.cfg["attention"] = {"thresholds": [90, 10], "half_life": "ошибка", "interrupt_margin": "много"}
    a = rig.att._att(rig.cfg)
    assert a["thresholds"] == [15, 35, 60, 85] and a["interrupt_margin"] == 15 and a["half_life"]["*"] == 300


# ── решения ATT-10 на границах порогов ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("p, d", [(0, "D0"), (14.99, "D0"), (15, "D1"), (34.99, "D1"), (35, "D2"), (59.99, "D2"),
                                  (60, "D3"), (84.99, "D3"), (85, "D4"), (100, "D4")])
def test_decision_thresholds(rig, p, d):
    assert rig.att._decide(p, rig.att._att(rig.cfg), away=False) == d


def test_thresholds_come_from_settings(rig):
    rig.cfg["attention"]["thresholds"] = [10, 20, 30, 40]
    assert rig.att._decide(25, rig.att._att(rig.cfg), False) == "D2" and rig.att._decide(40, rig.att._att(rig.cfg), False) == "D4"


def test_d4_by_margin_over_a_non_idle_focus(rig):
    att, a = rig.att, rig.att._att(rig.cfg)
    assert att._decide(70, a, False) == "D3"                          # фокус idle: D3 не превращается в D4
    att._push_focus("question", "q", "q", 45, True)
    assert att._decide(59.9, a, False) == "D2"                        # меньше 60 в D4 не попадает вообще
    assert att._decide(60, a, False) == "D3"                          # 60 > 45 + 15 неверно: граница включена в запас
    assert att._decide(60.1, a, False) == "D4"                        # p >= 60 и p > priority фокуса + 15
    att._push_focus("conversation", "разговор", "user", 90, True)
    assert att._decide(84, a, False) == "D3"                          # 90 + 15 недостижимо, а 85 ещё нет
    assert att._decide(85, a, False) == "D4"
    rig.cfg["attention"]["interrupt_margin"] = 30                     # запас из настроек
    att._focus.pop()
    assert att._decide(70, rig.att._att(rig.cfg), False) == "D3" and att._decide(75.1, rig.att._att(rig.cfg), False) == "D4"


def test_d2_to_d4_become_d1_when_user_is_away(rig):
    a = rig.att._att(rig.cfg)
    assert [rig.att._decide(p, a, away=True) for p in (10, 20, 40, 70, 95)] == ["D0", "D1", "D1", "D1", "D1"]


# ── путь события: echo.ping с urgency_hint 1, 3, 5 (приёмка 2) ─────────────────────────────────────

async def test_echo_ping_hints_1_3_5_give_d1_d3_d4_and_dnd_lowers(rig):
    rig.modules.declare("echo", emits=[("echo.ping", 1)])
    for hint, decision in ((1, "D1"), (3, "D3"), (5, "D4")):
        d = await rig.submit("echo", urgency_hint=hint, addressed_to_me=False, text=f"Пинг {hint}")
        assert d["decision"] == decision, hint
    # возраст чуть больше нуля (как в живой работе) держит тройку под границей 60: D2
    item = rig.att._make_item("echo", {"kind": "echo.ping", "text": "т", "urgency_hint": 3}, rig.clock.t - 1)
    item.urgency = 3
    s = rig.att._score(item, rig.cfg, rig.att._att(rig.cfg))
    assert 59 < s["priority"] < 60 and rig.att._decide(s["priority"], rig.att._att(rig.cfg), False) == "D2"
    # «не отвлекать»: тройка и пятёрка падают; пятёрка с обращением к ней звучит
    rig.cfg["dnd"] = True
    assert (await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="а"))["decision"] == "D1"
    assert (await rig.submit("echo", urgency_hint=5, addressed_to_me=False, text="б"))["decision"] == "D1"
    assert (await rig.submit("echo", urgency_hint=5, addressed_to_me=True, text="в"))["decision"] == "D4"


async def test_decision_is_logged_with_every_multiplier(rig):
    await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Т" * 300)
    rec = evt_log()[-1]
    assert len(rec["text"]) <= 200
    d = rec["data"]
    assert (d["module"], d["event_kind"], d["urgency"], d["priority"], d["decision"]) == ("echo", "echo.ping", 3, 60.0, "D3")
    assert {"w_profile", "w_sender", "w_rule", "w_user", "freshness", "classifier_ms", "item"} <= set(d)


# ── правила до классификатора R1…R4 ───────────────────────────────────────────────────────────────

OWNER = {"id": "tg:1", "name": "Аня", "is_owner": True}
CHAT = {"module": "tg", "target": "tg:1"}


async def test_r1_owner_address_goes_to_conversation_not_attention(rig):
    d = await rig.submit("tg", kind="tg.dm", text="Ты скоро?", addressed_to_me=True, sender=OWNER, reply_channel=CHAT)
    assert d["rule"] == "R1" and rig.io.inputs == [("Ты скоро?", CHAT, OWNER)]
    assert rig.candidates() == [] and rig.decider.calls == []         # классификатор не вызывался


async def test_r1_does_not_fire_without_address_owner_or_channel(rig):
    cases = [dict(addressed_to_me=True, sender={"id": "x", "is_owner": False}, reply_channel=CHAT),
             dict(addressed_to_me=False, sender=OWNER, reply_channel=CHAT),
             dict(addressed_to_me=True, sender=OWNER)]
    for ev in cases:
        d = await rig.submit("tg", kind="tg.news", text="а", urgency_hint=1, **ev)
        assert d["rule"] != "R1"
    assert rig.io.inputs == []


async def test_r1_direct_message_of_owner_counts_as_address(rig):
    d = await rig.submit("tg", kind="tg.dm", text="привет", sender=OWNER, reply_channel=CHAT)       # личка ей
    assert d["rule"] == "R1" and len(rig.io.inputs) == 1


async def test_r2_owner_reply_in_question_channel_answers_the_question(rig):
    q = asyncio.create_task(approvals.ask("approval", "Удалить?", timeout_s=30))
    await _until(approvals.has_pending)
    rig.att._question_channel = Channel("module", "tg", "owner")
    d = await rig.submit("tg", kind="tg.group", text="да", sender=OWNER, reply_channel=CHAT, addressed_to_me=False)
    assert d["rule"] == "R2" and await q == (True, "user")
    assert rig.io.inputs == []                                        # ответ ушёл в approvals, не в разговор


async def test_r2_ignored_for_other_channel_other_sender_or_a_plain_message(rig):
    q = asyncio.create_task(approvals.ask("approval", "Удалить?", timeout_s=30))
    await _until(approvals.has_pending)
    rig.att._question_channel = Channel("module", "tg", "owner")
    stranger = {"id": "tg:7", "name": "Чужой", "is_owner": False}
    for ev in (dict(sender=stranger, reply_channel=CHAT, text="да"),
               dict(sender=OWNER, reply_channel={"module": "other", "target": "x"}, text="да"),
               dict(sender=OWNER, reply_channel=CHAT, text="расскажи про погоду в Казани подробно")):
        d = await rig.submit("tg", kind="tg.group", urgency_hint=1, addressed_to_me=False, **ev)
        assert d["rule"] != "R2"
    assert approvals.has_pending()
    approvals.resolve(False)
    assert await q == (False, "user")


async def test_r3_mute_rule_sends_to_digest_without_classifier(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("danger")}))
    await rig.att.set_rule(module="tg", effect="mute", minutes=60)
    d = await rig.submit("tg", kind="tg.dm", text="срочно!", urgency_hint=5, sender={"id": "tg:9", "name": "Маша"})
    assert d["rule"] == "R3" and d["decision"] == "D1" and rig.decider.calls == []
    assert rig.att.digest_counts() == {"tg": 1} and rig.candidates() == []
    rig.clock.t += 61 * 60                                            # правило истекло
    d = await rig.submit("tg", kind="tg.dm", text="срочно!", urgency_hint=5, addressed_to_me=False)
    assert d["rule"] is None and d["decision"] == "D4"


async def test_r4_critical_declared_event_skips_classifier(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("spam")}))
    rig.modules.declare("rimworld", emits=[("rimworld.raid", 5), ("rimworld.letter", 2)])
    d = await rig.submit("rimworld", kind="rimworld.raid", text="Налёт!", urgency_hint=5)
    assert d["rule"] == "R4" and d["urgency"] == 5 and d["decision"] == "D4" and rig.decider.calls == []
    # подсказка 5, но модуль не объявлял такую срочность: обычный путь (с классификатором, он судит spam)
    d = await rig.submit("rimworld", kind="rimworld.letter", text="Письмо", urgency_hint=5)
    assert d["rule"] is None and rig.decider.calls and d["urgency"] == 0


# ── склейка повторов ATT-6 ────────────────────────────────────────────────────────────────────────

async def test_dedupe_key_merges_repeats_within_a_minute(rig):
    await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Пинг 1", dedupe_key="k")
    cand = rig.candidates()[0]
    assert "Пинг 1" in cand.text
    rig.clock.t += 30
    assert await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Пинг 2", dedupe_key="k") is not None
    assert len(rig.candidates()) == 1
    item = next(iter(rig.att._items.values()))
    assert item.count == 2 and item.text == "Пинг 2"                 # счётчик и новый текст
    assert "Пинг 2" in cand.text and "повторилось 2 раз" in cand.text
    rig.clock.t += 61                                                 # прошла минута без повторов: новое событие
    await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Пинг 3", dedupe_key="k")
    assert len(rig.att._items) == 2


async def test_dedupe_does_not_merge_after_the_event_was_spoken_or_across_modules(rig):
    await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Раз", dedupe_key="k")
    await rig.play()
    await rig.submit("echo", urgency_hint=3, addressed_to_me=False, text="Два", dedupe_key="k")
    assert len(rig.att._items) == 2                                   # первое уже озвучено
    await rig.submit("other", urgency_hint=3, addressed_to_me=False, text="Три", dedupe_key="k")
    assert len(rig.att._items) == 3


async def test_dedupe_updates_digest_entry(rig):
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, text="А", dedupe_key="k")
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, text="Б", dedupe_key="k")
    entry = rig.att._digest["echo"][0]
    assert len(rig.att._digest["echo"]) == 1 and entry["text"] == "Б" and entry["count"] == 2


# ── классификатор ATT-4a, ATT-8, ATT-32 ───────────────────────────────────────────────────────────

def danger_answers(text):
    return {"category": ans("danger", 0.73), "addressed": ans("group", 0.6), "needs_reply": ans("no", 0.6)}


async def test_classifier_not_called_when_module_gives_hint_and_address(tmp_path):
    rig = Rig(tmp_path, StubDecider(danger_answers))
    await rig.submit("rimworld", urgency_hint=2, addressed_to_me=False, text="Колонист уснул")
    assert rig.decider.calls == []


async def test_classifier_called_for_people_texts_and_missing_hints(tmp_path):
    rig = Rig(tmp_path, StubDecider(danger_answers))
    await rig.submit("tg", urgency_hint=3, addressed_to_me=True, sender={"id": "tg:5", "name": "Маша"}, text="Привет")
    await rig.submit("desktop", urgency_hint=2, text="Окно сменилось")         # addressed_to_me не задан
    await rig.submit("desktop", addressed_to_me=False, text="Окно сменилось 2")      # нет подсказки срочности
    assert len(rig.decider.calls) == 3
    assert rig.decider.calls[0][1] == list(EVENT_QUESTIONS)


async def test_urgency_is_max_of_hint_and_category(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("news", 0.9)}))          # новость: 1
    assert (await rig.submit("tg", sender={"id": "a"}, urgency_hint=3, text="т"))["urgency"] == 3
    rig.decider.answers = {"category": ans("danger", 0.5)}                   # опасность: 5
    assert (await rig.submit("tg", sender={"id": "a"}, urgency_hint=1, text="т2"))["urgency"] == 5
    rig.decider.answers = {"category": ans("routine", 0.9)}                  # рутина: 0, подсказка важнее
    assert (await rig.submit("tg", sender={"id": "a"}, urgency_hint=2, text="т3"))["urgency"] == 2
    rig.decider.answers = {"category": ans("question", 0.9)}                 # без подсказки берётся категория
    assert (await rig.submit("tg", sender={"id": "a"}, text="т4"))["urgency"] == 3


async def test_spam_gives_zero_regardless_of_hint(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("spam", 0.9)}))
    d = await rig.submit("tg", sender={"id": "a"}, urgency_hint=4, text="Скидка 70%")
    assert d["urgency"] == 0 and d["decision"] == "D0" and rig.candidates() == []


async def test_low_confidence_answer_is_ignored(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("danger", 0.34), "addressed": ans("assistant", 0.1)}))
    d = await rig.submit("tg", sender={"id": "a"}, urgency_hint=2, text="Что-то")
    assert d["urgency"] == 2                                          # подсказка модуля, а не категория
    rig.cfg["attention"]["min_conf_event"] = 0.3                      # порог в настройках
    d = await rig.submit("tg", sender={"id": "a"}, urgency_hint=2, text="Что-то ещё")
    assert d["urgency"] == 5


async def test_classifier_fills_address_and_needs_reply_unless_module_set_them(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("personal_message", 0.9), "addressed": ans("assistant", 0.9),
                                     "needs_reply": ans("yes", 0.9)}))
    await rig.submit("tg", kind="tg.dm", sender={"id": "tg:8", "name": "Маша"}, reply_channel=CHAT, urgency_hint=3, text="Ответь мне")
    item = next(iter(rig.att._items.values()))
    assert item.addressed_to_me is True and item.needs_reply is True
    rig.att._items.clear()
    await rig.submit("tg", kind="tg.dm", sender={"id": "tg:8", "name": "Маша"}, addressed_to_me=False, urgency_hint=3, text="Ответь мне 2")
    assert next(iter(rig.att._items.values())).addressed_to_me is False       # явное значение модуля сильнее


async def test_unavailable_classifier_falls_back_to_hints_and_default_one(tmp_path):
    rig = Rig(tmp_path, StubDecider(None))                            # сервис не отвечает
    assert (await rig.submit("tg", sender={"id": "a"}, urgency_hint=4, text="т"))["urgency"] == 4
    assert (await rig.submit("tg", sender={"id": "a"}, text="т2"))["urgency"] == 1
    rig.modules.declare("tg", emits=[("tg.news", 2)])
    assert (await rig.submit("tg", kind="tg.news", sender={"id": "a"}, text="т3"))["urgency"] == 2     # default_urgency манифеста
    item = next(iter(rig.att._items.values()))
    assert item.needs_reply is False


async def test_classifier_switch_in_settings(tmp_path):
    rig = Rig(tmp_path, StubDecider(danger_answers))
    rig.cfg["attention"]["classifier"] = False
    d = await rig.submit("tg", sender={"id": "a"}, urgency_hint=2, text="т")
    assert rig.decider.calls == [] and d["urgency"] == 2


async def test_long_classifier_queue_skips_classifier_for_new_events(tmp_path):
    rig = Rig(tmp_path, StubDecider(danger_answers))
    rig.decider.gate = asyncio.Event()                                # классификатор «думает»
    for i in range(20):
        await rig.att.submit("tg", {"kind": "tg.dm", "text": f"т{i}", "sender": {"id": "a"}, "urgency_hint": 2})
    await asyncio.sleep(0)
    assert rig.att._cls_pending == 20 and len(rig.decider.calls) == 20
    await rig.att.submit("tg", {"kind": "tg.dm", "text": "двадцать первый", "sender": {"id": "a"}})
    await rig.att.submit("tg", {"kind": "tg.dm", "text": "двадцать второй", "sender": {"id": "a"}, "urgency_hint": 3})
    assert len(rig.decider.calls) == 20                               # новые без классификатора
    last = [d for d in rig.att.decisions][-2:]
    assert [d["urgency"] for d in last] == [1, 3]                     # без подсказки 1, с подсказкой она
    rig.decider.gate.set()
    await rig.att.settle()
    assert rig.att._cls_pending == 0


async def test_classification_does_not_block_the_event_queue(tmp_path):
    rig = Rig(tmp_path, StubDecider(danger_answers))
    rig.decider.gate = asyncio.Event()
    await asyncio.wait_for(rig.att.submit("tg", {"kind": "tg.dm", "text": "т", "sender": {"id": "a"}}), 1)   # submit вернулся
    assert rig.att.last_decision is None                              # решения ещё нет: ждём ответа
    rig.decider.gate.set()
    await rig.att.settle()
    assert rig.att.last_decision["urgency"] == 5


# ── что делает решение ────────────────────────────────────────────────────────────────────────────

async def test_d1_goes_to_digest_and_memorable_to_graph(rig):
    graph = SimpleNamespace(items=[], enqueue=lambda s, t: graph.items.append((s, t)))
    rig.companion.graph = graph
    await rig.submit("tg", kind="tg.dm", text="Маша переезжает в Казань", urgency_hint=1, addressed_to_me=False,
                     memorable=True, sender={"id": "tg:2", "name": "Маша"})
    assert graph.items == [("Маша", "Маша переезжает в Казань")] and rig.att.digest_counts() == {"tg": 1}
    await rig.submit("tg", kind="tg.dm", text="пустяк", urgency_hint=0, addressed_to_me=False, memorable=True)
    assert len(graph.items) == 1                                      # D0: даже memorable не попадает в граф


async def test_d2_d3_make_fact_candidate_with_priority_and_expiry(rig):
    await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text="Пинг", expires_in_s=100, dedupe_key="k")
    cand = rig.candidates()[0]
    assert (cand.mode, cand.priority, cand.expires - rig.clock.t, cand.source.startswith("attention:")) == ("fact", 40, 100, True)
    assert "Событие модуля «echo» (echo.ping): Пинг" in cand.text and cand.key == "attention:echo:k"
    await rig.submit("echo", kind="echo.other", urgency_hint=3, addressed_to_me=False, text="Другое")
    assert rig.candidates()[1].priority == 60 and rig.candidates()[1].expires - rig.clock.t == 600   # срок по умолчанию


async def test_d4_pushes_reaction_focus_and_pops_it_after_speech(rig):
    d = await rig.submit("echo", urgency_hint=5, addressed_to_me=False, text="Пожар!")
    assert d["decision"] == "D4"
    top = rig.att.top_focus()
    assert top.kind == "reaction" and top.priority == 100 and "Пожар" in top.topic
    assert rig.companion.presence.snapshot()["focus"].startswith("reaction: ") and rig.companion.presence.snapshot()["busy"]
    await rig.play()
    assert rig.att.top_focus().kind == "idle" and not rig.companion.presence.snapshot()["busy"]
    assert rig.companion.about and "Пожар" in rig.companion.about[0][0]


async def test_reaction_focus_pops_when_candidate_expires_and_event_goes_to_digest(rig):
    await rig.submit("echo", urgency_hint=5, addressed_to_me=False, text="Пожар!", expires_in_s=30)
    rig.clock.t += 31
    await rig.att.speech.pump()
    assert rig.att.top_focus().kind == "idle"                         # реакция снята вместе с устаревшим кандидатом
    assert rig.att.digest_counts() == {"echo": 1}                     # не потерялось: устаревшее нерассказанное идёт в сводку
    await rig.submit("echo", kind="echo.b", urgency_hint=2, addressed_to_me=False, text="Мелочь", expires_in_s=30)
    rig.clock.t += 31
    await rig.att.speech.pump()
    assert rig.att.digest_counts() == {"echo": 2}                     # ATT-26: и D2 тоже


async def test_reply_note_when_event_can_be_answered(tmp_path):
    rig = Rig(tmp_path, StubDecider({"category": ans("question", 0.9), "needs_reply": ans("no", 0.5)}))
    rig.modules.declare("tg", title="Телеграм")
    await rig.submit("tg", kind="tg.dm", text="Ты скоро?", sender={"id": "tg:5", "name": "Маша"}, reply_channel=CHAT,
                     urgency_hint=3, addressed_to_me=False)
    fact = rig.candidates()[0].text
    assert "ответить через модуль «Телеграм»" in fact and "только если пользователь попросит" in fact
    await rig.submit("tg", kind="tg.dm", text="Ты скоро 2?", sender={"id": "tg:5", "name": "Маша"}, urgency_hint=3,
                     addressed_to_me=False)                           # канала ответа нет
    assert "ответить" not in rig.candidates()[1].text
    rig.decider.answers = {"category": ans("routine", 0.9), "needs_reply": ans("yes", 0.9)}
    await rig.submit("tg", kind="tg.news", text="Новость", sender={"id": "tg:5"}, reply_channel=CHAT, urgency_hint=0)
    assert rig.att.last_decision["decision"] == "D0" and len(rig.candidates()) == 2        # D0: озвучивать нечего


# ── речь модулей ──────────────────────────────────────────────────────────────────────────────────

async def test_module_speech_goes_through_the_same_scoring(rig):
    await rig.att.submit_speech("rimworld", {"text": "Борис уснул.", "mode": "verbatim", "emotion": "angry", "key": "c"})
    cand = rig.candidates()[0]
    assert (cand.source, cand.mode, cand.text, cand.emotion, cand.priority, cand.key) == \
        ("module:rimworld", "verbatim", "Борис уснул.", "angry", 40, "module:rimworld:c")
    assert cand.expires - rig.clock.t == 20                           # готовая реплика модуля живёт 20 с
    await rig.att.submit_speech("rimworld", {"text": "Налёт", "mode": "fact", "urgency_hint": 5, "expires_in_s": 15})
    hot = rig.candidates()[1]
    assert (hot.mode, hot.priority, hot.expires - rig.clock.t) == ("fact", 100, 15) and rig.att.top_focus().kind == "reaction"
    await rig.att.submit_speech("rimworld", {"text": "  "})
    assert len(rig.candidates()) == 2


async def test_module_speech_is_muted_dropped_in_dnd_and_weighted_by_profile(tmp_path):
    rig = Rig(tmp_path, weights={"rimworld.*": 1.5})
    await rig.att.submit_speech("rimworld", {"text": "а", "mode": "verbatim"})
    assert rig.candidates()[0].priority == 60                         # 40 * 1.5
    rig.cfg["dnd"] = True
    await rig.att.submit_speech("rimworld", {"text": "б", "mode": "verbatim"})
    assert len(rig.candidates()) == 1 and rig.att.digest_counts() == {"rimworld": 1}      # 18: в сводку
    await rig.att.set_rule(module="rimworld", effect="mute", minutes=10)
    rig.cfg["dnd"] = False
    await rig.att.submit_speech("rimworld", {"text": "в", "mode": "verbatim"})
    assert len(rig.candidates()) == 1 and rig.att.digest_counts() == {"rimworld": 2}


# ── задачи ATT-12 ─────────────────────────────────────────────────────────────────────────────────

async def test_job_updates_done_failed_stage_and_waiting_user(rig):
    base = {"job_id": "j-00000001", "title": "Рендер"}
    await rig.att.on_job_update("montage", {**base, "state": "running", "stage": "a"})
    assert rig.att.last_decision["urgency"] == 1 and rig.att.last_decision["decision"] == "D1"        # стадия: в сводку
    await rig.att.on_job_update("montage", {**base, "state": "running", "stage": "a", "progress": 0.5})
    assert len(rig.att.decisions) == 1                                # тот же этап: ничего нового
    await rig.att.on_job_update("montage", {**base, "state": "waiting_user", "stage": "a", "question": {"question_id": "q"}})
    assert len(rig.att.decisions) == 1                                # вопрос задачи задаёт шина
    await rig.att.on_job_update("montage", {**base, "state": "done", "stage": "z", "text": "Готово: 48 секунд."})
    d = rig.att.last_decision
    assert d["urgency"] == 3 and d["decision"] == "D3" and d["event_kind"] == "montage.job"
    fact = rig.candidates()[-1].text
    assert "Рендер" in fact and "выполнена" in fact and "48 секунд" in fact
    await rig.att.on_job_update("montage", {**base, "state": "done", "stage": "z"})
    assert len(rig.att.decisions) == 2                                # повтор той же смены не дублируется
    await rig.att.on_job_update("montage", {"job_id": "j-00000002", "state": "failed", "error": {"message": "нет места"}})
    assert rig.att.last_decision["urgency"] == 4 and "нет места" in rig.candidates()[-1].text
    assert rig.candidates()[-1].priority == 80


async def test_job_update_respects_mute_rule_and_job_half_life(rig):
    await rig.att.set_rule(kind="montage.*", effect="mute", minutes=5)
    await rig.att.on_job_update("montage", {"job_id": "j-1", "title": "Т", "state": "done"})
    assert rig.att.last_decision["rule"] == "R3" and rig.candidates() == []
    assert rig.att._half_life("montage.job", rig.att._att(rig.cfg)) == 600


# ── стек фокуса ATT-20…ATT-23 ─────────────────────────────────────────────────────────────────────

def test_focus_bottom_is_idle_and_cannot_be_removed(rig):
    att = rig.att
    assert att.top_focus().kind == "idle" and att.top_focus().priority == 0
    att._pop_focus(att._focus[0].id)
    assert len(att._focus) == 1
    assert rig.companion.presence.snapshot()["focus"] == "idle" and not rig.companion.presence.snapshot()["busy"]


def test_focus_depth_is_three_oldest_resumable_goes_first(rig):
    att = rig.att
    a = att._push_focus("conversation", "разговор", "user", 90, True)
    b = att._push_focus("reaction", "r1", "x", 70, False)
    c = att._push_focus("reaction", "r2", "x", 70, False)
    d = att._push_focus("reaction", "r3", "x", 70, False)
    ids = [f.id for f in att._focus[1:]]
    assert len(ids) == 3 and a.id not in ids and ids == [b.id, c.id, d.id] or a.id in ids
    assert len(att._focus) - 1 <= 3
    # самый старый resumable удаляется первым: разговор ушёл, реакции остались
    assert [f.kind for f in att._focus[1:]] == ["reaction", "reaction", "reaction"]
    att._push_focus("question", "q", "x", 50, True)
    assert len(att._focus) - 1 == 3 and att._focus[-1].kind == "question"


async def test_user_input_pushes_conversation_which_expires_after_a_minute(rig):
    await rig.att.on_user_input("Привет, как дела?")
    f = rig.att.top_focus()
    assert (f.kind, f.priority, f.resumable) == ("conversation", 90, True) and f.topic == "Привет, как дела?"
    assert rig.companion.presence.snapshot()["focus"] == "conversation: Привет, как дела?"
    rig.clock.t += 59
    await rig.att.on_user_input("ещё одно сообщение")                   # продлевает, второго фокуса нет
    assert len(rig.att._focus) == 2 and rig.att.top_focus().topic == "ещё одно сообщение"
    rig.clock.t += 59
    rig.att._focus_tick(rig.clock.t)
    assert rig.att.top_focus().kind == "conversation"
    rig.clock.t += 2
    rig.att._focus_tick(rig.clock.t)
    assert rig.att.top_focus().kind == "idle"


async def test_conversation_reply_extends_the_conversation(rig):
    from conversation_io import DASHBOARD, current_channel
    await rig.att.on_user_input("вопрос")
    rig.clock.t += 50
    token = current_channel.set(DASHBOARD)
    try:
        cand = rig.att.enqueue_say("Ответ", None, None)
    finally:
        current_channel.reset(token)
    assert (cand.source, cand.priority) == ("conversation", 95)
    rig.clock.t += 50                                                 # 100 с от вопроса, но 50 от ответа
    rig.att._focus_tick(rig.clock.t)
    assert rig.att.top_focus().kind == "conversation"


async def test_interrupted_conversation_returns_as_a_fact_candidate(rig):
    await rig.att.on_user_input("давай обсудим отпуск")
    await rig.submit("echo", urgency_hint=5, addressed_to_me=False, text="Пожар!")      # D4 поверх разговора
    assert [f.kind for f in rig.att._focus[1:]] == ["conversation", "reaction"]
    await rig.play()                                                  # реплика о пожаре прозвучала, реакция снята
    back = [c for c in rig.candidates() if c.source == "attention:return"]
    assert len(back) == 1 and back[0].priority == 40 and back[0].mode == "fact"
    assert "давай обсудим отпуск" in back[0].text and "Пожар" in back[0].text and "промолчать" in back[0].text
    assert rig.att.top_focus().kind == "conversation"


async def test_no_return_fact_for_an_old_conversation(rig):
    await rig.att.on_user_input("давно говорили")
    rig.clock.t += 40
    await rig.submit("echo", urgency_hint=5, addressed_to_me=False, text="Пожар!")
    rig.clock.t += 270                                                # разговор моложе 60 с не жив, но проверяем окно 300 с
    rig.att._focus[1].last_active = rig.clock.t - 301
    await rig.play(advance=0)
    assert [c for c in rig.candidates() if c.source == "attention:return"] == []


async def test_question_focus_lives_while_waiting(rig):
    task = asyncio.create_task(rig.att.submit_question(Question("approval", "Удалить?", source="montage", timeout_s=30)))
    await _until(lambda: rig.att.top_focus().kind == "question")
    assert rig.att.top_focus().priority == 60 and rig.companion.presence.snapshot()["focus"].startswith("question: ")
    await rig.att.settle()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert rig.att.top_focus().kind == "idle"


# ── временные правила ATT-24 ──────────────────────────────────────────────────────────────────────

async def test_set_rule_requires_a_selector(rig):
    with pytest.raises(ToolError):
        await rig.att.set_rule(effect="mute", minutes=10)


async def test_each_rule_effect_and_expiry(rig):
    text = await rig.att.set_rule(module="tg", kind="tg.dm", effect="boost", minutes=15)
    assert "Правило создано" in text and "boost" in text and "15 мин" in text
    assert rig.att.rules.weight("tg", "tg.dm", None) == 2.0
    rig.clock.t += 15 * 60 + 1
    assert rig.att.rules.weight("tg", "tg.dm", None) == 1.0 and rig.att.rules.active() == []
    assert rig.att.rules_text() == "Временных правил внимания сейчас нет."


async def test_normal_cancels_matching_rules(rig):
    await rig.att.set_rule(module="tg", effect="mute", minutes=60)
    await rig.att.set_rule(module="tg", kind="tg.dm", effect="boost", minutes=60)
    await rig.att.set_rule(module="rimworld", effect="mute", minutes=60)
    text = await rig.att.set_rule(module="tg", effect="normal")
    assert "сняты" in text and "2" in text
    assert [r.module for r in rig.att.rules.active()] == ["rimworld"]


async def test_rules_listing_and_clearing(rig):
    await rig.att.set_rule(sender="мама", effect="only", minutes=120)
    await rig.att.set_rule(module="echo", effect="mute")
    listing = rig.att.rules_text()
    assert "only" in listing and "мама" in listing and "echo" in listing and "ещё 120 мин" in listing
    first = rig.att.rules.active()[0].id
    assert await rig.att.clear_rules(first) == "Снято правил: 1."
    assert await rig.att.clear_rules() == "Снято правил: 1."
    assert rig.att.rules.active() == []


async def test_rule_minutes_are_clamped_and_remember_the_user_phrase(rig):
    await rig.att.on_user_input("час не трогай меня с чатами")
    await rig.att.set_rule(module="tg", effect="mute", minutes=99999)
    rule = rig.att.rules.active()[0]
    assert rule.until - rig.clock.t == 1440 * 60 and rule.created_from == "час не трогай меня с чатами"
    await rig.att.set_rule(module="tg2", effect="mute", minutes=0)
    assert min(r.until for r in rig.att.rules.active()) - rig.clock.t == 60


async def test_rules_survive_restart_and_expired_are_dropped_on_load(tmp_path):
    rig = Rig(tmp_path)
    await rig.att.set_rule(module="tg", sender="мама", effect="only", minutes=10)
    await rig.att.set_rule(module="echo", effect="mute", minutes=100)
    other = Attention(rig.companion, rig.io, clock=rig.clock, settings_source=lambda: rig.cfg,
                      rules_file=tmp_path / "rules.json", autopump=False)
    assert {(r.module, r.effect) for r in other.rules.active()} == {("tg", "only"), ("echo", "mute")}
    rig.clock.t += 11 * 60                                            # через 11 минут первое правило просрочено
    later = Attention(rig.companion, rig.io, clock=rig.clock, settings_source=lambda: rig.cfg,
                      rules_file=tmp_path / "rules.json", autopump=False)
    assert [(r.module, r.effect) for r in later.rules.active()] == [("echo", "mute")]


async def test_tick_purges_expired_rules_once_a_minute(rig):
    await rig.att.set_rule(module="tg", effect="mute", minutes=1)
    rig.clock.t += 61
    await rig.att.tick()
    assert rig.att.rules.rules == []
    assert rig.att.rules.path.read_text(encoding="utf-8").strip() == "[]"       # и из файла тоже


async def test_broken_rules_file_does_not_break_core(tmp_path):
    (tmp_path / "rules.json").write_text("{не json", encoding="utf-8")
    assert Rig(tmp_path).att.rules.active() == []


# ── сводка ATT-26, ATT-27 ─────────────────────────────────────────────────────────────────────────

async def test_digest_buffer_keeps_fifty_per_module_and_take_clears(rig):
    for i in range(55):
        await rig.submit("echo", urgency_hint=1, addressed_to_me=False, text=f"событие {i}")
    assert rig.att.digest_counts() == {"echo": 50}
    await rig.submit("other", urgency_hint=1, addressed_to_me=False, text="чужое")
    text = rig.att.take_digest("echo")
    lines = text.splitlines()
    assert "- echo: событие 54" in lines and "- echo: событие 4" not in lines and "- echo: событие 5" in lines
    assert "чужое" not in text                                        # вытеснены самые старые: остались события 5…54
    assert rig.att.digest_counts() == {"other": 1}
    assert "чужое" in rig.att.take_digest() and "пусто" in rig.att.take_digest()


async def test_digest_sorted_by_priority(rig):
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, text="мелочь")
    await rig.submit("echo", kind="echo.b", urgency_hint=1, addressed_to_me=False, text="важнее", expires_in_s=0.001)
    rig.att._digest["echo"][1]["priority"] = 33
    assert rig.att.take_digest().index("важнее") < rig.att.take_digest("echo").find("мелочь") + 1000


async def fill_digest(rig, n=4, urgency=1, module="echo"):
    for i in range(n):
        await rig.submit(module, urgency_hint=urgency, addressed_to_me=False, text=f"факт {i}", kind=f"{module}.k{i}")


async def test_digest_offered_when_focus_returns_to_idle_with_enough_important_entries(rig):
    await fill_digest(rig, 4, urgency=1)                              # priority 20 < 25: в предложение не идут
    await rig.att.on_user_input("привет")
    rig.clock.t += 61
    rig.att._focus_tick(rig.clock.t)
    assert [c for c in rig.candidates() if c.source == "attention:digest"] == []
    # три записи с priority 25 и выше
    rig.cfg["attention"]["thresholds"] = [15, 50, 60, 85]
    rig.att._digest.clear()
    for i in range(4):
        await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text=f"факт {i}", kind=f"echo.k{i}")     # 40: D1
    assert rig.att.digest_counts() == {"echo": 4}
    await rig.att.on_user_input("привет 2")
    rig.clock.t += 61
    rig.att._focus_tick(rig.clock.t)
    offers = [c for c in rig.candidates() if c.source == "attention:digest"]
    assert len(offers) == 1 and offers[0].priority == 40 and offers[0].text.startswith("Накопилось:")
    assert offers[0].mode == "fact" and offers[0].text.count("факт") == 4


async def test_digest_offer_has_at_most_five_facts_and_only_once(rig):
    rig.cfg["attention"]["thresholds"] = [15, 50, 60, 85]
    for i in range(8):
        await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text=f"факт {i}", kind=f"echo.k{i}")
    rig.att._digest["echo"][7]["priority"] = 49
    await rig.att.on_user_input("привет")
    rig.clock.t += 61
    rig.att._focus_tick(rig.clock.t)
    offer = [c for c in rig.candidates() if c.source == "attention:digest"][0]
    assert offer.text.count("факт") == 5
    await rig.att.on_user_input("снова")                             # снова разговор и тишина, но уже предлагали
    rig.clock.t += 61
    rig.att._focus_tick(rig.clock.t)
    assert len([c for c in rig.candidates() if c.source == "attention:digest"]) == 1
    assert rig.att.digest_counts() == {"echo": 8}                     # записи остались до attention.digest


async def test_digest_not_offered_after_recent_own_remark(rig):
    rig.cfg["attention"]["thresholds"] = [15, 50, 60, 85]
    for i in range(4):
        await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text=f"факт {i}", kind=f"echo.k{i}")
    rig.att.speech.last_initiative = rig.clock.t - 300                # недавно говорила сама
    await rig.att.on_user_input("привет")
    rig.clock.t += 61
    rig.att._focus_tick(rig.clock.t)
    assert [c for c in rig.candidates() if c.source == "attention:digest"] == []


# ── вопросы ATT-19 ────────────────────────────────────────────────────────────────────────────────

async def _until(predicate, timeout=2.0):
    async def waiter():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(waiter(), timeout)


async def test_question_voiced_then_waits_then_answered(rig):
    q = Question("choice", "Утвердить план?", ["утвердить", "переделать"], timeout_s=30, source="montage", urgency_hint=4)
    task = asyncio.create_task(rig.att.submit_question(q))
    await _until(lambda: rig.candidates())
    cand = rig.candidates()[0]
    assert (cand.source, cand.priority, cand.mode, cand.must, cand.question) == ("question", 80, "fact", True, True)
    assert "Утвердить план?" in cand.text and "montage" in cand.text and "переделать" in cand.text
    assert approvals.has_pending() and approvals.is_announcing()      # ещё не прозвучал: ответа не ждут
    await rig.play()
    assert rig.companion.about[-1][1:] == (True, True)
    await _until(lambda: not approvals.is_announcing())
    assert approvals.has_pending() and approvals.pending_kind() == "choice"
    assert await approvals.try_answer("переделать")
    assert await task == ("переделать", "user")
    assert rig.att.top_focus().kind == "idle"


@pytest.mark.parametrize("hint, priority", [(None, 60), (0, 40), (1, 40), (2, 40), (3, 60), (4, 80), (5, 90), (9, 90)])
async def test_question_priority_is_urgency_times_twenty_between_40_and_90(rig, hint, priority):
    task = asyncio.create_task(rig.att.submit_question(Question("approval", "?", urgency_hint=hint, timeout_s=30)))
    await _until(lambda: rig.candidates())
    assert rig.candidates()[0].priority == priority
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_while_a_question_waits_other_speech_is_blocked_but_conversation_is_not(rig):
    task = asyncio.create_task(rig.att.submit_question(Question("approval", "Можно?", timeout_s=30)))
    await _until(lambda: rig.candidates())
    await rig.play()
    await _until(lambda: not approvals.is_announcing())
    await rig.submit("echo", urgency_hint=4, addressed_to_me=False, text="Событие во время вопроса")
    assert await rig.play() is None                                   # условие 3: ждёт ответа на вопрос
    rig.att.speech.new(source="conversation", mode="verbatim", text="Ответ пользователю", priority=95)
    assert (await rig.play()).text == "Ответ пользователю"
    approvals.resolve(True)
    assert await task == (True, "user")
    assert (await rig.play()).source.startswith("attention:")          # вопрос закрыт: событие, наконец, звучит


async def test_answer_before_the_question_was_voiced_removes_it_from_the_queue(rig):
    task = asyncio.create_task(rig.att.submit_question(Question("approval", "Можно?", timeout_s=30)))
    await _until(lambda: rig.candidates())
    assert approvals.answer(True)                                     # кнопка в дашборде
    assert await task == (True, "user")
    assert rig.candidates() == []                                     # старый вопрос озвучивать не будем


async def test_question_goes_to_telegram_when_user_is_away(rig):
    conn = SimpleNamespace(module_id="telegram", manifest={"capabilities": ["chat.channel"]})
    rig.att.hub = SimpleNamespace(connections=lambda: [conn])
    rig.modules.declare("telegram")
    rig.idle = 700
    task = asyncio.create_task(rig.att.submit_question(Question("approval", "Удалить рендеры?", source="montage", timeout_s=30)))
    await _until(lambda: rig.companion.about)
    fact, must, question = rig.companion.about[0]
    assert "Удалить рендеры?" in fact and must and question
    assert rig.companion.about_channels[0] == Channel("module", "telegram", "owner")      # ответ уйдёт deliver в Telegram
    assert rig.candidates() == []                                     # вслух никто не слушает
    # ответ владельца из Telegram приходит событием: правило R2
    d = await rig.submit("telegram", kind="telegram.group", text="да", sender=OWNER, reply_channel={"module": "telegram", "target": "tg:1"},
                         addressed_to_me=False)
    assert d["rule"] == "R2" and await task == (True, "user")


async def test_question_stays_in_voice_when_user_is_near_or_no_channel_module(rig):
    conn = SimpleNamespace(module_id="telegram", manifest={"capabilities": ["chat.channel"]})
    rig.att.hub = SimpleNamespace(connections=lambda: [conn])
    for idle, hub in ((599, rig.att.hub), (700, SimpleNamespace(connections=lambda: []))):
        rig.idle, rig.att.hub = idle, hub
        task = asyncio.create_task(rig.att.submit_question(Question("approval", "?", timeout_s=30)))
        await _until(lambda: rig.candidates())
        assert rig.candidates()[0].source == "question" and rig.companion.about == []
        approvals.answer(True)
        await task
        rig.att.speech.items.clear()


# ── обращения пользователя ATT-18, ATT-28 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["Замолчи", "замолчи!", "помолчи пожалуйста", "хватит болтать", "Тихо", "стоп", "Пожалуйста, замолчи"])
def test_stop_phrases(text):
    assert Attention.looks_like_stop(text)


@pytest.mark.parametrize("text", ["расскажи про тихий океан", "не замолчи, а скажи ещё", "стоп-кран в метро это что", "Как дела?", ""])
def test_not_stop_phrases(text):
    assert not Attention.looks_like_stop(text)


async def test_stop_phrase_stops_speech_and_clears_queue_before_any_model_call(rig):
    rig.att.speech.new(source="attention:a1", mode="verbatim", text="говорит", priority=50)
    await rig.att.speech.pump()
    await rig.att.settle()
    rig.att.speech.new(source="attention:a2", mode="fact", text="ждёт", priority=60)
    rig.att.speech.new(source="attention:a3", mode="fact", text="срочное", priority=90)
    await rig.att.on_user_input("Замолчи")
    assert rig.companion.speech.stopped[-1] is None                   # SpeechOutput.stop(None)
    assert [c.text for c in rig.candidates()] == ["срочное"]          # ниже 85 снято
    assert evt_log()[-1]["data"]["stop"] is True


async def test_classifier_stop_intent_also_stops(tmp_path):
    rig = Rig(tmp_path, StubDecider({"intent": ans("stop", 0.53), "needs_memory": ans("no", 0.7)}))
    rig.att.speech.new(source="attention:a2", mode="fact", text="ждёт", priority=60)
    cls = await rig.att.classify_input("Хватит")
    assert cls.intent == "stop" and rig.candidates() == [] and rig.companion.speech.stopped


async def test_classify_input_threshold_and_switch(tmp_path):
    rig = Rig(tmp_path, StubDecider({"intent": ans("action", 0.48), "needs_memory": ans("yes", 0.9)}))
    cls = await rig.att.classify_input("Открой браузер")
    assert cls.intent is None and cls.needs_memory is True            # intent ниже 0.5 не используется
    rig.cfg["attention"]["classifier"] = False
    assert await rig.att.classify_input("Открой браузер") is None
    rig.cfg["attention"]["classifier"] = True
    rig.decider.answers = None
    assert await rig.att.classify_input("Открой браузер") is None     # сервис недоступен
    assert rig.decider.calls[0][1] == ["intent", "needs_memory"]


async def test_enqueue_say_by_context(rig):
    from conversation_io import VOICE, current_channel
    cand = rig.att.enqueue_say("сама", None, None)
    assert (cand.source, cand.priority) == ("proactive", 30)
    cand = rig.att.enqueue_say("сама", None, None, source="proactive")
    assert cand.expires - rig.clock.t == 60
    token = current_channel.set(VOICE)
    try:
        cand = rig.att.enqueue_say("ответ", "happy", None)
        fact = rig.att.enqueue_fact("итог", "просьба", must=True)
    finally:
        current_channel.reset(token)
    assert (cand.source, cand.priority, cand.channel, cand.emotion) == ("conversation", 95, VOICE, "happy")
    assert (fact.source, fact.mode, fact.must, fact.user_text, fact.expires - rig.clock.t) == ("conversation", "fact", True, "просьба", 120)
    token = approvals.announcing.set(True)
    try:
        asked = rig.att.enqueue_say("Разрешаешь?", None, None)
    finally:
        approvals.announcing.reset(token)
    assert asked.source == "question" and asked.priority == 80


async def test_user_speaking_flag_goes_to_presence(rig):
    rig.att.set_user_speaking(True)
    assert rig.companion.presence.snapshot()["user_speaking"] is True
    rig.att.set_user_speaking(False)
    assert rig.companion.presence.snapshot()["user_speaking"] is False


async def test_snapshot_for_dashboard(rig):
    await rig.att.set_rule(module="tg", effect="mute", minutes=30)
    await rig.submit("echo", urgency_hint=2, addressed_to_me=False, text="Пинг")
    snap = rig.att.snapshot()
    assert snap["rules"][0]["module"] == "tg" and snap["rules"][0]["minutes_left"] == 30
    assert snap["queue"][0]["priority"] == 40 and snap["focus"][0]["kind"] == "idle" and snap["settings"]["classifier"]
    assert snap["decisions"][-1]["decision"] == "D2"


async def test_event_path_without_classifier_is_fast(rig):
    """ATT-31: правила, формула и решение без классификатора - единицы миллисекунд (запись в журнал входит в замер)."""
    import time
    rig.modules.declare("echo", emits=[("echo.ping", 2)])
    t0 = time.perf_counter()
    n = 200
    for i in range(n):
        await rig.att.submit("echo", {"kind": "echo.ping", "text": f"Пинг {i}", "urgency_hint": 1, "addressed_to_me": False})
    per_event_ms = (time.perf_counter() - t0) * 1000 / n
    assert per_event_ms < 5.0, per_event_ms


# ── TG-14: отправитель любого события попадает в relationships и память до решения ──────────────────────

class RecRel(FakeRel):
    def __init__(self, people=None, fail=False):
        super().__init__(people)
        self.calls: list[tuple] = []
        self.fail = fail

    def ensure_person(self, key, name, source="", **extra):
        if self.fail:
            raise OSError("диск недоступен")
        self.calls.append(("ensure", key, name, source))
        new = key not in self.people
        self.people.setdefault(key, {"name": name, "affinity": 0.0})
        return new

    def note(self, key, text):
        self.calls.append(("note", key, text))


class RecMem:
    def __init__(self):
        self.facts: list[tuple] = []

    def add_fact(self, text, kind="user", importance=3, due_days=None):
        self.facts.append((text, kind, importance))
        return True


def tg14_rig(rig, rel=None):
    rig.companion._rel = rel or RecRel()
    rig.companion._mem = RecMem()
    rig.companion.graph = SimpleNamespace(items=[], enqueue=lambda s, t: rig.companion.graph.items.append((s, t)))
    return rig.companion._rel, rig.companion._mem, rig.companion.graph


MASHA = {"id": "tg:5", "name": "Маша", "is_owner": False}
FACT = "Сообщение от Маша в личке бота: «я переезжаю в Казань»"


async def test_tg14_sender_is_registered_noted_and_memorized_even_when_the_event_is_dropped(rig):
    rel, mem, graph = tg14_rig(rig)
    d = await rig.submit("telegram", kind="telegram.bot_message", text=FACT, urgency_hint=0, addressed_to_me=False,
                         memorable=True, sender=MASHA, reply_channel={"module": "telegram", "target": "bot:5"})
    assert d["decision"] == "D0"                                           # озвучивать нечего, а запомнить нужно
    assert rel.calls == [("ensure", "tg:5", "Маша", "telegram"), ("note", "tg:5", FACT)]
    assert graph.items == [("Маша", FACT)] and mem.facts == []      # сырое событие в долгую память не пишется (его запомнит эпизод)


async def test_tg14_memory_is_written_once_when_the_event_is_also_spoken_about(rig):
    rel, mem, graph = tg14_rig(rig)
    await rig.submit("telegram", kind="telegram.bot_message", text=FACT, urgency_hint=3, addressed_to_me=True,
                     memorable=True, sender=MASHA)
    assert rig.att.last_decision["decision"] in ("D2", "D3", "D4")
    assert len(graph.items) == 1 and mem.facts == []                     # _act второй раз в граф не кладёт


async def test_tg14_not_memorable_event_only_registers_the_person(rig):
    rel, mem, graph = tg14_rig(rig)
    await rig.submit("telegram", kind="telegram.group", text="Сообщение от Маша в группе «Соседи»: «всем привет»",
                     urgency_hint=1, addressed_to_me=False, sender=MASHA)
    assert [c[0] for c in rel.calls] == ["ensure", "note"] and graph.items == [] and mem.facts == []


async def test_tg14_needs_sender_id_and_name(rig):
    rel, mem, graph = tg14_rig(rig)
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, memorable=True)
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, memorable=True, sender={"id": "echo:1"})
    await rig.submit("echo", urgency_hint=1, addressed_to_me=False, memorable=True, sender={"name": "Безымянный"})
    assert rel.calls == [] and mem.facts == []
    assert len(graph.items) == 3                                            # граф берёт прежний путь _act (D1 и выше), не TG-14


async def test_tg14_owner_address_registers_person_but_conversation_writes_the_memory(rig):
    rel, mem, graph = tg14_rig(rig)
    owner = {"id": "tg:777", "name": "Аня", "is_owner": True}
    d = await rig.submit("telegram", kind="telegram.bot_message", text="Сообщение от Аня в личке бота: «как дела?»",
                         addressed_to_me=True, memorable=True, sender=owner, reply_channel={"module": "telegram", "target": "bot:777"})
    assert d["rule"] == "R1" and len(rig.io.inputs) == 1
    assert [c[:2] for c in rel.calls] == [("ensure", "tg:777"), ("note", "tg:777")]
    assert graph.items == [] and mem.facts == []                            # реплику в граф и память кладёт разговор


async def test_tg14_duplicate_within_a_minute_is_not_registered_twice(rig):
    rel, mem, graph = tg14_rig(rig)
    for _ in range(2):
        await rig.submit("telegram", kind="telegram.bot_message", text=FACT, urgency_hint=2, addressed_to_me=True,
                         memorable=True, sender=MASHA, dedupe_key="tg:bot:5:10")
    assert [c[0] for c in rel.calls] == ["ensure", "note"] and len(graph.items) == 1 and mem.facts == []


async def test_tg14_stranger_weighs_08_only_for_the_first_message(rig):
    tg14_rig(rig)
    await rig.submit("telegram", kind="telegram.dm", text="т", urgency_hint=2, addressed_to_me=False, sender=MASHA)
    assert rig.att.last_decision["w_sender"] == 0.8                          # профиль заведён этим событием: ещё «неизвестный»
    await rig.submit("telegram", kind="telegram.dm", text="т2", urgency_hint=2, addressed_to_me=False, sender=MASHA)
    assert rig.att.last_decision["w_sender"] == 1.0                          # теперь известный человек


async def test_tg14_storage_failure_does_not_stop_the_event(rig):
    tg14_rig(rig, RecRel(fail=True))
    d = await rig.submit("telegram", kind="telegram.dm", text="т", urgency_hint=2, addressed_to_me=False, sender=MASHA)
    assert d["decision"] in ("D1", "D2", "D3", "D4")
