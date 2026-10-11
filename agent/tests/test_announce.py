"""Объявления о долгих запусках (AN-1…AN-3, правило 14 ТЗ): звучат всегда, мимо формулы важности и паузы между репликами.
Фиктивные часы, заглушки вывода речи и соединения с модулем, без sleep."""
from types import SimpleNamespace

import pytest
from test_attention import Rig, evt_log

import module_registry
from attention import Attention
from environments.registry import Registry
from glados_module import protocol as P
from speech_queue import ANNOUNCE_P, SpeechCandidate


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def just_spoke(rig):
    """Только что была самостоятельная реплика: обычному кандидату пауза min_gap не даст говорить."""
    rig.att.speech.last_initiative = rig.clock.t


async def launch(rig, text="Запускаю RimWorld для записи забега.", module="rimworld", **event):
    await rig.att.submit(module, {"kind": f"{module}.launch", "text": text, **event})
    await rig.att.settle()


# ── AN-1: вид кандидата ─────────────────────────────────────────────────────────────────────────

async def test_announce_candidate_has_priority_75_ttl_180_and_cannot_be_skipped(rig):
    await launch(rig)
    (cand,) = rig.candidates()
    assert cand.family == "announce" and cand.priority == ANNOUNCE_P == 75
    assert cand.expires - cand.created == 180.0 and cand.must is True and cand.mode == "fact"


async def test_announce_ignores_min_gap_but_a_regular_fact_waits(rig):
    just_spoke(rig)
    rig.att.speech.new(source="attention:x", mode="fact", text="обычный факт", priority=70)
    await launch(rig)
    assert await rig.play(advance=1.0) is not None
    assert [a[0] for a in rig.companion.about][0].startswith("Объявление о запуске")
    assert len(rig.companion.about) == 1                           # обычный (тот же приоритет D3) ещё ждёт паузу
    assert [c.text for c in rig.candidates()] == ["обычный факт"]


async def test_announce_is_not_dropped_as_a_repeat(rig):
    rig.companion.repeats = True                                   # «недавно сказанное»: обычное снимается, объявление нет
    rig.att.speech.new(source="attention:x", mode="fact", text="повтор", priority=70)
    await launch(rig)
    await rig.play()
    assert len(rig.companion.about) == 1 and rig.companion.about[0][0].startswith("Объявление")
    await rig.play()
    assert len(rig.companion.about) == 1 and rig.candidates() == []   # повтор снят, не сказан


async def test_announce_goes_before_chatter_and_regular_candidates(rig):
    rig.att.speech.new(source="proactive", mode="verbatim", text="болтовня", priority=30)
    rig.att.speech.new(source="attention:y", mode="fact", text="факт D2", priority=55)
    await launch(rig)
    first = await rig.play()
    assert first.family == "announce"


async def test_announce_does_not_interrupt_the_current_reply_but_follows_it(rig):
    reply = SpeechCandidate("s-r", "conversation", "verbatim", "ответ", None, 95, rig.clock.t, rig.clock.t + 99)
    rig.att.speech.current, rig.companion.speaking = reply, True   # звучит ответ разговора (95)
    await launch(rig)
    assert await rig.att.speech.pump() is None and rig.companion.speech.stopped == []      # не обрывает
    rig.companion.speaking = False
    cand = await rig.att.speech.pump()
    await rig.att.settle()
    assert cand is not None and cand.family == "announce" and rig.companion.speech.stopped == []


async def test_announce_interrupts_only_chatter(rig):
    chatter = SpeechCandidate("s-c", "proactive", "verbatim", "болтаю", None, 30, rig.clock.t, rig.clock.t + 99)
    rig.att.speech.current, rig.companion.speaking = chatter, True
    await launch(rig)
    cand = await rig.att.speech.pump()
    await rig.att.settle()
    assert cand is not None and cand.family == "announce" and rig.companion.speech.stopped        # «болтовню» прервала


async def test_unspoken_announce_expires_into_the_digest(rig):
    rig.companion.presence.update(user_speaking=True)               # пользователь говорит: объявление ждёт
    await launch(rig)
    assert await rig.att.speech.pump() is None
    rig.clock.t += 181
    await rig.att.speech.pump()
    assert rig.candidates() == [] and "Запускаю RimWorld" in rig.att.take_digest()


async def test_stop_phrase_moves_a_waiting_announce_to_the_digest(rig):
    rig.companion.presence.update(user_speaking=True)
    await launch(rig)
    await rig.att.on_user_input("Замолчи")
    assert rig.candidates() == [] and "Запускаю RimWorld" in rig.att.take_digest()


# ── AN-1: склейка ───────────────────────────────────────────────────────────────────────────────

async def test_identical_announcements_within_20s_are_merged_with_a_counter(rig):
    await launch(rig)
    rig.clock.t += 10
    await launch(rig)
    await launch(rig)
    (cand,) = rig.candidates()
    assert "3 раз" in cand.text
    rig.clock.t += 11                                               # окно считается от первого объявления: прошло 21 с
    await launch(rig)
    assert len(rig.candidates()) == 2
    await launch(rig, text="Запускаю другое.")                      # другой текст не склеивается
    await launch(rig, module="montage")                             # другой источник тоже
    assert len(rig.candidates()) == 4


async def test_repeat_after_it_was_spoken_is_swallowed_inside_the_window(rig):
    await launch(rig)
    await rig.play(advance=1.0)
    await launch(rig)
    assert rig.candidates() == [] and len(rig.companion.about) == 1
    assert evt_log()[-1]["data"]["merged"] is True and evt_log()[-1]["data"]["count"] == 2


# ── AN-2: «не отвлекать» и отсутствие пользователя ───────────────────────────────────────────────

@pytest.mark.parametrize("how", ["dnd", "quiet_until"])
async def test_announce_sounds_briefly_when_do_not_disturb(rig, how):
    if how == "dnd":
        rig.cfg["dnd"] = True
    else:
        rig.cfg["quiet_until"] = rig.clock.t + 600
    await launch(rig)
    assert rig.att.last_decision["route"] == "short"
    await rig.play(advance=1.0)
    fact = rig.companion.about[0][0]
    assert "Коротко" in fact and rig.companion.about[0][1] is True


async def test_announce_without_dnd_is_not_marked_short(rig):
    await launch(rig)
    await rig.play(advance=1.0)
    assert "Коротко" not in rig.companion.about[0][0] and rig.att.last_decision["route"] == "speech"


async def test_announce_goes_to_digest_when_user_is_away(rig):
    rig.idle = 601
    await launch(rig)
    assert rig.candidates() == [] and rig.att.last_decision["route"] == "digest"
    assert rig.att.digest_counts() == {"rimworld": 1}
    assert await rig.att.speech.pump() is None and rig.companion.about == []
    assert "Запускаю RimWorld" in rig.att.take_digest()


async def test_announce_ignores_mute_rules(rig):
    rig.att.rules.add(module="rimworld", kind=None, sender=None, effect="mute", minutes=30)
    await launch(rig)
    assert len(rig.candidates()) == 1


async def test_decision_announce_is_logged(rig):
    await launch(rig)
    d = evt_log()[-1]["data"]
    assert (d["decision"], d["rule"], d["priority"], d["module"], d["event_kind"]) == ("ANNOUNCE", "ANNOUNCE", 75.0, "rimworld",
                                                                                      "rimworld.launch")
    assert d["route"] == "speech" and rig.att.last_decision["decision"] == "ANNOUNCE"


# ── AN-2: речь модуля и событие с пометкой ───────────────────────────────────────────────────────

async def test_say_with_announce_flag_is_an_announcement_even_with_low_urgency(rig):
    just_spoke(rig)
    await rig.att.submit_speech("montage", {"text": "Начинаю финальный рендер.", "mode": "fact", "urgency_hint": 0,
                                           "announce": True})
    (cand,) = rig.candidates()
    assert cand.family == "announce" and cand.priority == 75 and rig.att.last_decision["decision"] == "ANNOUNCE"
    # без пометки та же реплика с urgency_hint 0 до очереди не доходит (D0)
    await rig.att.submit_speech("montage", {"text": "Другое.", "mode": "fact", "urgency_hint": 0})
    assert len(rig.candidates()) == 1


async def test_verbatim_say_announcement_is_spoken_as_is(rig):
    await rig.att.submit_speech("rimworld", {"text": "Запускаю игру.", "mode": "verbatim", "emotion": "calm",
                                            "announce": True})
    await rig.play()
    assert rig.companion.said == [("Запускаю игру.", "calm")]


async def test_event_with_announce_flag_and_any_kind(rig):
    await rig.att.submit("desktop", {"kind": "desktop.app", "text": "Открываю браузер.", "announce": True,
                                     "urgency_hint": 0})
    await rig.att.settle()
    assert [c.family for c in rig.candidates()] == ["announce"]


async def test_non_announce_event_is_unchanged(rig):
    await rig.att.submit("rimworld", {"kind": "rimworld.letter", "text": "Письмо", "urgency_hint": 1, "addressed_to_me": False})
    await rig.att.settle()
    assert rig.candidates() == [] and rig.att.last_decision["decision"] == "D1"


# ── AN-3: инструмент с announce: true ───────────────────────────────────────────────────────────

TOOLS = [
    {"name": "render", "description": "Собрать финальный ролик. Долго, до получаса.", "params": {
        "title": {"type": "string"}, "context": {"type": "string", "required": False}}, "risk": "auto",
     "availability": "always", "announce": True, "core_fills": ["context"]},
    {"name": "peek", "description": "Быстро посмотреть состояние.", "params": {}, "risk": "auto", "availability": "always"},
]


class FakeConn:
    def __init__(self, order):
        self.order = order

    async def request(self, msg_type, body, timeout=0):
        self.order.append(("call", body["tool"]))
        return "готово"


class FakeAttention:
    def __init__(self, order, fail=False):
        self.order, self.fail, self.announces = order, fail, []

    async def submit_announce(self, module_id, text, *, data=None):
        self.order.append(("announce", text))
        if self.fail:
            raise RuntimeError("внимание сломано")
        self.announces.append((module_id, text, data))


def make_registry(order, attention):
    reg = module_registry.ModuleRegistry(Registry(), settings_source=lambda: {})
    reg.hub = SimpleNamespace(connection=lambda mid: FakeConn(order), attention=attention)
    manifest = {"id": "montage", "title": "Монтаж", "description": "Монтаж видео", "version": "0.1.0", "tools": TOOLS}
    reg._build_environment(reg.declare("montage"), P.Manifest.from_dict(manifest))
    return reg


async def test_announce_tool_announces_before_the_call_with_short_arguments():
    order = []
    att = FakeAttention(order)
    reg = make_registry(order, att)
    assert await reg.envs.get("montage.render").call({"title": "Т" * 100, "context": "длинный контекст разговора"}) == "готово"
    assert [o[0] for o in order] == ["announce", "call"]                  # объявление ДО вызова
    (module, text, data) = att.announces[0]
    assert module == "montage" and data == {"tool": "render"}
    assert text.startswith("Запускаю render: Собрать финальный ролик.") and "Аргументы: title=" in text
    assert "Т" * 41 not in text and "контекст" not in text               # аргументы короткие, служебные (core_fills) не называются
    assert not text.endswith("..")


async def test_quick_tool_is_silent_and_a_broken_announcement_does_not_stop_the_call():
    order = []
    reg = make_registry(order, FakeAttention(order))
    await reg.envs.get("montage.peek").call({})
    assert order == [("call", "peek")]
    order.clear()
    reg = make_registry(order, FakeAttention(order, fail=True))
    assert await reg.envs.get("montage.render").call({"title": "Х"}) == "готово"
    assert [o[0] for o in order] == ["announce", "call"]


async def test_core_announce_reaches_the_speech_queue_through_the_manager(rig):
    await rig.att.submit_announce("montage", "Запускаю render: Собрать финальный ролик.", data={"tool": "render"})
    (cand,) = rig.candidates()
    assert cand.family == "announce" and "Запускаю render" in cand.text
    assert isinstance(rig.att, Attention) and rig.att.last_decision["event_kind"] == "montage.launch"
