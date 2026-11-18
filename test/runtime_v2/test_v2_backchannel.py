"""Поддакивания человека в звонке («угу», «отлично, хороший план»): отдельный пассивный вид, фон; не будят и не доливаются в ход."""
import pytest

from services.attention_v2.attention import AttentionV2
from services.attention_v2.eventlog import EventLog
from services.attention_v2.kinds import KindRegistry, from_legacy
from services.attention_v2.routing import VOICE_BACKCHANNEL_KIND, register_core_kinds
from services.attention_v2.testing import FakeClock, FakeRuntime
from services.voice_engine import events as vevents


def _utt(text, reason="owner", addressed=True):
    return vevents.utterance_event(text=text, speaker={"id": "u1", "name": "Маша", "is_owner": True}, session="s1", adapter="discord",
                                   duration_ms=500, language="ru", addressed=addressed, reason=reason)


@pytest.mark.parametrize("text,want", [
    ("Угу.", True), ("Отлично, это крутая идея.", True), ("Отлично, хороший план.", True), ("Класс!", True), ("ага, понятно", True),
    ("Поясни.", False), ("Да.", False), ("Нет", False), ("Лично. Что будешь дальше?", False), ("Это хорошо?", False),
    ("Глэдос, отлично", False), ("Ладно, ты там рассказывай, что происходит.", False), ("", False),
    ("угу угу угу угу угу угу угу", False),                      # слишком длинно для поддакивания
])
def test_is_backchannel(text, want):
    assert vevents.is_backchannel(text) is want


def test_backchannel_gets_passive_kind_and_background_priority():
    reg = KindRegistry()
    register_core_kinds(reg)
    e = from_legacy("voicechat", _utt("Угу."), reg)
    assert reg.normalize(e).accepted
    assert e.kind == VOICE_BACKCHANNEL_KIND and e.priority == 10
    assert reg.get(VOICE_BACKCHANNEL_KIND).passive


def test_name_and_real_phrases_stay_user_priority():
    reg = KindRegistry()
    register_core_kinds(reg)
    named = from_legacy("voicechat", _utt("Угу", reason="name"), reg)            # обращение по имени остаётся обращением
    real = from_legacy("voicechat", _utt("Поясни."), reg)
    for e in (named, real):
        reg.normalize(e)
        assert e.kind == "voicechat.utterance" and e.priority == 100


async def test_backchannel_is_not_steered_into_user_turn_and_never_wakes():
    clock = FakeClock()
    reg = KindRegistry()
    register_core_kinds(reg)
    runtime = FakeRuntime(clock=clock)
    v2 = AttentionV2(reg, EventLog(clock=clock), runtime, clock, lambda: "discord")
    runtime.attention = v2
    real = from_legacy("voicechat", _utt("Что будешь делать дальше?"), reg, now=clock.time())
    await v2.process_event(real)
    await clock.advance(1)                                                    # окно слияния: ход запущен по обращению человека
    assert runtime.triggers and runtime.can_steer is not None
    steers_before = len(getattr(runtime, "steers", []))
    ack = from_legacy("voicechat", _utt("Отлично, хороший план."), reg, now=clock.time())
    await v2.process_event(ack)
    assert len(getattr(runtime, "steers", [])) == steers_before              # поддакивание в идущий ход не доливается
    assert ack in v2.queue.items()                                            # лежит в очереди до ближайшей пачки
