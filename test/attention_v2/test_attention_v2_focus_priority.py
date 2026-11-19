"""Агент в RimWorld: гибель и другие важные события игры не ждут конца ответа человеку, а доливаются в идущий USER-ход сразу."""
from services.attention_v2.event import USER
from services.attention_v2.testing import Rig


async def test_critical_game_event_is_steered_into_a_user_turn():
    r = Rig(focus="rimworld")
    r.runtime.set_batch([r.ev(USER, workspace="discord", kind="discord.voice.utterance", group_key="s:u1")])
    death = await r.send(70, workspace="rimworld", kind="rimworld.alert")          # критичный вид игры
    assert r.runtime.steers == [death] and death not in r.attention.queue


async def test_important_event_of_the_focused_environment_is_steered_into_a_user_turn():
    r = Rig(focus="rimworld")
    r.runtime.set_batch([r.ev(USER, workspace="discord", kind="discord.voice.utterance", group_key="s:u1")])
    e = await r.send(70, workspace="rimworld", kind="rimworld.event")
    assert r.runtime.steers == [e]


async def test_important_event_of_another_environment_still_waits_during_a_user_turn():
    r = Rig(focus="rimworld")
    r.runtime.set_batch([r.ev(USER, workspace="discord", kind="discord.voice.utterance", group_key="s:u1")])
    e = await r.send(70, workspace="discord", kind="discord.delivery_failed")
    assert r.runtime.steers == [] and e in r.attention.queue


async def test_weak_event_of_the_focus_waits_and_burst_is_rate_limited():
    r = Rig(focus="rimworld")
    r.runtime.set_batch([r.ev(USER, workspace="discord", kind="discord.voice.utterance", group_key="s:u1")])
    weak = await r.send(30, workspace="rimworld", kind="rimworld.event")
    assert r.runtime.steers == [] and weak in r.attention.queue
    first = await r.send(70, workspace="rimworld", kind="rimworld.alert")
    second = await r.send(70, workspace="rimworld", kind="rimworld.alert")          # тот же вид сразу: в очередь, не заваливает ход
    assert r.runtime.steers == [first] and second in r.attention.queue
    await r.clock.advance(1)
    third = await r.send(70, workspace="rimworld", kind="rimworld.alert")
    assert r.runtime.steers == [first, third]
