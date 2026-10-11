"""Очередь реплик и воспроизведение (VOICE-3, VOICE-4, VOICE-20): события, порядок, замена, очередь, остановка, тишина."""
import asyncio

from speaker import MAX_WAITING, Speaker
from voice_rig import FakePlayer, FakeSynth, until


class Rig:
    def __init__(self, player=None, synth=None, muted=False):
        self.events: list[dict] = []
        self.player = player or FakePlayer()
        self.synth = synth or FakeSynth()
        self.is_muted = muted
        self.heard: list[str] = []
        self.sp = Speaker(synth=self.synth, player=self.player, emit=self.events.append, volume=lambda: 0.5,
                          muted=lambda: self.is_muted, on_segment=self.heard.append)
        self.sp.start()

    def phases(self, uid=None):
        return [(e["phase"], e.get("seq"), e.get("completed"), e.get("reason")) for e in self.events
                if uid is None or e["utterance_id"] == uid]

    async def ended(self, uid):
        await until(lambda: any(e["phase"] == "ended" and e["utterance_id"] == uid for e in self.events))
        return next(e for e in self.events if e["phase"] == "ended" and e["utterance_id"] == uid)


async def test_speak_emits_started_segment_ended_in_order():
    r = Rig()
    await r.sp.speak("u-1", "Первое. Второе! Третье?")
    end = await r.ended("u-1")
    assert r.phases("u-1") == [("started", 0, None, None), ("segment", 1, None, None), ("segment", 2, None, None),
                               ("ended", None, True, None)]
    assert [e["segment"] for e in r.events if e["phase"] != "ended"] == ["Первое.", "Второе!", "Третье?"]
    assert end["completed"] is True and end["spoken_upto"] == 3 and "reason" not in end
    assert r.heard == ["Первое.", "Второе!", "Третье?"]                 # слух не слушает собственные предложения
    assert r.player.volumes == [0.5, 0.5, 0.5] and not r.sp.is_speaking() and not r.sp.audible
    await r.sp.aclose()


async def test_next_segment_is_synthesized_while_current_plays():
    synth, player = FakeSynth(), FakePlayer(hold=True)
    r = Rig(player=player, synth=synth)
    await r.sp.speak("u-1", "Первое. Второе. Третье.")
    await until(lambda: len(player.played) == 1)
    await until(lambda: len(synth.calls) == 2)                           # второе уже синтезируется, пока звучит первое
    assert len(player.played) == 1 and r.sp.audible
    player.finish()
    await until(lambda: len(player.played) == 2)
    await until(lambda: len(synth.calls) == 3)
    player.finish()
    await until(lambda: len(player.played) == 3)
    player.finish()
    assert (await r.ended("u-1"))["completed"]
    await r.sp.aclose()


async def test_replace_true_interrupts_current_at_once_and_clears_waiting():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Долго говорю. Ещё долго.")
    await until(lambda: len(player.played) == 1)
    await r.sp.speak("u-2", "Ждёт.")                                      # в очереди
    await r.sp.speak("u-3", "Срочно!", replace=True)
    end1 = await r.ended("u-1")
    assert end1["completed"] is False and end1["reason"] == "replaced" and end1["spoken_upto"] == 0
    assert (await r.ended("u-2"))["reason"] == "replaced"
    assert player.cancelled == 1                                           # воспроизведение убито сразу
    await until(lambda: len(player.played) == 2)
    player.finish()
    assert (await r.ended("u-3"))["completed"]
    await r.sp.aclose()


async def test_replace_false_queues_after_current():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Первая.")
    await r.sp.speak("u-2", "Вторая.")
    await until(lambda: len(player.played) == 1)
    assert r.sp.current_uid == "u-1" and r.sp.waiting_uids() == ["u-2"]
    player.finish()
    await r.ended("u-1")
    await until(lambda: len(player.played) == 2)
    assert [e["utterance_id"] for e in r.events if e["phase"] == "started"] == ["u-1", "u-2"]
    player.finish()
    await r.ended("u-2")
    await r.sp.aclose()


async def test_queue_overflow_drops_the_oldest_waiting():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-0", "Звучит.")
    await until(lambda: len(player.played) == 1)
    for i in range(1, MAX_WAITING + 2):                                    # на одну больше, чем помещается
        await r.sp.speak(f"u-{i}", f"Реплика {i}.")
    dropped = await r.ended("u-1")
    assert dropped["reason"] == "replaced" and dropped["completed"] is False
    assert r.sp.waiting_uids() == ["u-2", "u-3", "u-4"]
    await r.sp.aclose()


async def test_stop_now_aborts_playback_and_ends_stopped():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Раз. Два. Три.")
    await until(lambda: len(player.played) == 1)
    await r.sp.stop("u-1", "now")
    end = await r.ended("u-1")
    assert end["completed"] is False and end["reason"] == "stop" and end["spoken_upto"] == 0
    assert player.cancelled == 1 and len(player.played) == 1 and not r.sp.is_speaking()
    await r.sp.aclose()


async def test_stop_segment_end_finishes_the_sentence_then_stops():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Раз. Два. Три.")
    await until(lambda: len(player.played) == 1)
    await r.sp.stop("u-1", "segment_end")
    await asyncio.sleep(0.05)
    assert player.cancelled == 0 and not any(e["phase"] == "ended" for e in r.events)      # предложение дослушивается
    player.finish()
    end = await r.ended("u-1")
    assert end["completed"] is False and end["reason"] == "stop" and end["spoken_upto"] == 1
    assert len(player.played) == 1                                          # следующие не звучат
    await r.sp.aclose()


async def test_stop_segment_end_on_the_last_sentence_is_completed():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Единственное предложение.")
    await until(lambda: len(player.played) == 1)
    await r.sp.stop("u-1", "segment_end")
    player.finish()
    end = await r.ended("u-1")
    assert end["completed"] is True and end["spoken_upto"] == 1
    await r.sp.aclose()


async def test_stop_null_clears_the_whole_queue():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Звучит. Ещё.")
    await r.sp.speak("u-2", "Ждёт.")
    await r.sp.speak("u-3", "Тоже ждёт.")
    await until(lambda: len(player.played) == 1)
    await r.sp.stop(None, "now")
    for uid in ("u-1", "u-2", "u-3"):
        assert (await r.ended(uid))["reason"] == "stop"
    assert not r.sp.is_speaking() and r.sp.waiting_uids() == []
    await r.sp.aclose()


async def test_stop_of_a_waiting_or_unknown_utterance():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Звучит.")
    await r.sp.speak("u-2", "Ждёт.")
    await until(lambda: len(player.played) == 1)
    await r.sp.stop("u-9", "now")                                          # чужой id: ничего
    assert r.sp.current_uid == "u-1" and r.sp.waiting_uids() == ["u-2"]
    await r.sp.stop("u-2", "segment_end")
    assert (await r.ended("u-2"))["reason"] == "stop" and r.sp.current_uid == "u-1"
    await r.sp.aclose()


async def test_stop_while_synthesizing_the_first_sentence_cancels_at_once():
    synth = FakeSynth(delay=5)
    r = Rig(synth=synth)
    await r.sp.speak("u-1", "Пока думаю.")
    await until(lambda: synth.calls)
    await r.sp.stop("u-1", "segment_end")                                  # слышимого предложения нет: договаривать нечего
    end = await r.ended("u-1")
    assert end["reason"] == "stop" and r.player.played == []
    assert not any(e["phase"] == "started" for e in r.events)
    await r.sp.aclose()


async def test_muted_gives_instant_started_and_ended_muted():
    r = Rig(muted=True)
    await r.sp.speak("u-1", "Тихая реплика. Вторая.")
    assert r.phases("u-1") == [("started", 0, None, None), ("ended", None, False, "muted")]
    assert r.events[0]["segment"] == "Тихая реплика. Вторая."                # облачко питомца видит весь текст
    assert r.events[1]["spoken_upto"] == 0
    assert r.player.played == [] and r.synth.calls == [] and not r.sp.is_speaking()
    await r.sp.aclose()


async def test_mute_now_aborts_current_and_waiting():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    await r.sp.speak("u-1", "Звучит.")
    await r.sp.speak("u-2", "Ждёт.")
    await until(lambda: len(player.played) == 1)
    r.sp.mute_now()
    assert (await r.ended("u-1"))["reason"] == "muted" and (await r.ended("u-2"))["reason"] == "muted"
    await r.sp.aclose()


async def test_synthesis_failure_skips_the_sentence_and_reports_error():
    synth = FakeSynth()
    synth.fail = {"Сломано."}
    r = Rig(synth=synth)
    await r.sp.speak("u-1", "Хорошо. Сломано. Снова хорошо.")
    end = await r.ended("u-1")
    assert end["completed"] is False and end["reason"] == "error" and end["spoken_upto"] == 2
    assert [e["seq"] for e in r.events if e["phase"] in ("started", "segment")] == [0, 2]
    await r.sp.aclose()


async def test_nothing_synthesized_ends_with_error_and_no_started():
    synth = FakeSynth()
    synth.fail = {"Раз."}
    r = Rig(synth=synth)
    await r.sp.speak("u-1", "Раз.")
    end = await r.ended("u-1")
    assert end["reason"] == "error" and not any(e["phase"] == "started" for e in r.events)
    await r.sp.aclose()


async def test_player_failure_is_reported_as_error():
    player = FakePlayer()
    player.fail_next = True
    r = Rig(player=player)
    await r.sp.speak("u-1", "Колонок нет.")
    end = await r.ended("u-1")
    assert end["reason"] == "error" and end["completed"] is False
    await r.sp.speak("u-2", "Снова.")
    assert (await r.ended("u-2"))["completed"]                              # воркер жив
    await r.sp.aclose()


async def test_empty_text_ends_with_error():
    r = Rig()
    await r.sp.speak("u-1", "   ")
    assert r.phases("u-1") == [("ended", None, False, "error")]
    await r.sp.aclose()


async def test_audible_flag_follows_playback():
    player = FakePlayer(hold=True)
    r = Rig(player=player)
    assert not r.sp.audible
    await r.sp.speak("u-1", "Звук.")
    await until(lambda: r.sp.audible)
    player.finish()
    await r.ended("u-1")
    assert not r.sp.audible
    await r.sp.aclose()
