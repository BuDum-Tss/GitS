"""Очередь речи (ТЗ 03, ATT-13…ATT-18): выбор по приоритету, условия запуска, сроки, повторы, прерывание, «замолчи».
Фиктивные часы и вывод речи в памяти, без sleep."""
import asyncio

from hub_helpers import FakeSpeech

from speech_queue import SpeechQueue


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Rig:
    """Очередь + заглушки: что сказано, присутствие, ответ на вопрос, разговорчивость."""

    def __init__(self, chattiness="normal"):
        self.clock = Clock()
        self.speech = FakeSpeech()
        self.presence = {"user_speaking": False, "last_user_ts": 0.0}
        self.chattiness = chattiness
        self.question = False
        self.repeat_texts: set[str] = set()
        self.played: list[str] = []
        self.dropped: list[tuple[str, str]] = []
        self.hold: asyncio.Event | None = None            # если задан, play ждёт его (реплика «готовится»)
        self.q = SpeechQueue(speech=self.speech, play=self._play, presence=lambda: self.presence,
                             chattiness=lambda: self.chattiness, question_pending=lambda: self.question,
                             repeats=lambda t: t in self.repeat_texts, clock=self.clock,
                             on_drop=lambda c, why: self.dropped.append((c.text, why)))

    async def _play(self, cand):
        if self.hold is not None:
            await self.hold.wait()
        self.played.append(cand.text)
        self.speech.speaking = True                        # реплика звучит, пока тест не скажет иначе
        return "u-" + cand.id

    def add(self, text, priority, source="proactive", mode="verbatim", **kw):
        kw.setdefault("ttl", 1000.0)
        return self.q.new(source=source, mode=mode, text=text, priority=priority, **kw)

    async def pump(self):
        cand = await self.q.pump()
        await self.q.idle()
        return cand


async def test_best_by_priority_then_oldest():
    r = Rig()
    r.add("низкий", 40, source="attention:a1")
    r.clock.t += 1
    r.add("высокий", 70, source="attention:a2")
    r.clock.t += 1
    r.add("высокий поздний", 70, source="attention:a3")
    order = []
    for _ in range(3):
        cand = await r.pump()
        order.append(cand.text)
        r.speech.speaking = False
        r.clock.t += 100                                   # min_gap не мешает
    assert order == ["высокий", "высокий поздний", "низкий"]


async def test_condition_1_waits_while_speaking():
    r = Rig()
    r.speech.speaking = True
    r.add("ждёт", 50, source="attention:a1")
    assert await r.pump() is None and r.played == []
    r.speech.speaking = False
    assert (await r.pump()).text == "ждёт"


async def test_condition_1_waits_while_reply_is_being_prepared():
    r = Rig()
    r.hold = asyncio.Event()
    r.add("готовится", 50, source="attention:a1")
    assert (await r.q.pump()).text == "готовится"
    r.add("второй", 50, source="attention:a2")
    r.clock.t += 100
    assert await r.q.pump() is None                        # первая ещё формируется: вторая не стартует
    r.hold.set()
    await r.q.idle()
    assert r.played == ["готовится"]


async def test_condition_2_user_speaking_and_tail():
    r = Rig()
    r.add("реплика", 50, source="attention:a1")
    r.presence["user_speaking"] = True
    assert await r.pump() is None
    r.presence["user_speaking"] = False
    r.presence["last_user_ts"] = r.clock.t - 1.0           # договорил секунду назад: рано (нужно 1,5 с)
    assert await r.pump() is None
    r.presence["last_user_ts"] = r.clock.t - 1.5
    assert (await r.pump()).text == "реплика"


async def test_conversation_reply_waits_only_for_user_to_stop_not_for_the_tail():
    r = Rig()
    r.add("ответ", 95, source="conversation")
    r.presence["last_user_ts"] = r.clock.t - 0.2            # только что написал: ответ сразу
    assert (await r.pump()).text == "ответ"
    r.speech.speaking = False
    r.add("ещё ответ", 95, source="conversation")
    r.presence["user_speaking"] = True
    assert await r.pump() is None                           # пока говорит, ответ ждёт


async def test_condition_3_question_pending_blocks_all_but_conversation_and_question():
    r = Rig()
    r.question = True
    r.add("болтовня", 30)
    r.add("факт", 70, source="attention:a1")
    assert await r.pump() is None
    r.add("ответ", 95, source="conversation")
    assert (await r.pump()).text == "ответ"
    r.speech.speaking = False
    r.add("формулировка вопроса", 40, source="question", mode="fact")
    assert (await r.pump()).text == "формулировка вопроса"


async def test_condition_4_min_gap_by_chattiness():
    for level, gap in (("quiet", 90), ("normal", 30), ("chatty", 10)):
        r = Rig(level)
        r.add("первая", 50, source="attention:a1")
        await r.pump()
        r.speech.speaking = False
        r.add("вторая", 50, source="attention:a2")
        r.clock.t += gap - 0.1
        assert await r.pump() is None, level
        r.clock.t += 0.2
        assert (await r.pump()).text == "вторая", level


async def test_condition_4_does_not_apply_to_conversation_question_high_priority_and_module_verbatim():
    r = Rig("quiet")
    r.add("первая", 50, source="attention:a1")
    await r.pump()
    cases = (("ответ", 95, "conversation", "verbatim"), ("вопрос", 50, "question", "fact"),
             ("срочное", 85, "attention:a2", "fact"), ("реплика игры", 50, "module:rimworld", "verbatim"))
    for text, prio, src, mode in cases:
        r.speech.speaking = False
        r.add(text, prio, source=src, mode=mode)
        assert (await r.pump()).text == text
    r.speech.speaking = False
    r.add("факт от модуля", 50, source="module:rimworld", mode="fact")
    assert await r.pump() is None                           # факт модуля (а не готовая реплика) подчиняется паузе


async def test_expired_candidates_are_dropped_with_journal():
    r = Rig()
    r.add("устарело", 50, source="attention:a1", ttl=10)
    r.clock.t += 10.1
    assert await r.pump() is None
    assert r.dropped == [("устарело", "expired")] and r.q.items == []


async def test_default_ttl_by_kind_of_candidate():
    r = Rig()
    c = r.q.new(source="conversation", mode="verbatim", text="ответ", priority=95)
    v = r.q.new(source="module:x", mode="verbatim", text="реплика модуля", priority=50)
    f = r.q.new(source="attention:a1", mode="fact", text="факт события", priority=50)
    p = r.q.new(source="proactive", mode="verbatim", text="проактивная", priority=30)
    t0 = r.clock.t
    assert (c.expires - t0, v.expires - t0, f.expires - t0, p.expires - t0) == (120, 20, 600, 60)


async def test_recent_repeat_is_dropped():
    r = Rig()
    r.repeat_texts.add("то же самое")
    r.add("то же самое", 50, source="attention:a1")
    r.add("новое", 40, source="attention:a2")
    assert (await r.pump()).text == "новое"
    assert ("то же самое", "repeat") in r.dropped
    r.speech.speaking = False
    r.add("то же самое", 95, source="conversation")
    assert (await r.pump()).text == "то же самое"           # ответ пользователю повтором не считается


async def test_same_key_keeps_newest():
    r = Rig()
    r.add("старый", 50, source="attention:a1", key="k")
    r.add("новый", 50, source="attention:a1", key="k")
    assert [c.text for c in r.q.items] == ["новый"] and r.dropped == [("старый", "replaced")]


async def test_overflow_drops_the_least_important():
    r = Rig()
    for i in range(55):
        r.add(f"c{i}", 10 + i, source="attention:a")
    assert len(r.q.items) == 50
    assert min(c.priority for c in r.q.items) == 15


async def test_high_priority_interrupts_lower_speech_by_margin():
    r = Rig()
    r.add("болтовня", 40, source="attention:a1")
    await r.pump()
    assert r.speech.speaking
    r.add("почти важное", 85, source="attention:a2")        # 85 - 40 >= 15: прерывает
    assert (await r.pump()).text == "почти важное"
    assert r.speech.stopped == ["u-" + r.q.dropped[0][0]] or len(r.speech.stopped) == 1
    assert any(why == "interrupted" for _, _, why in r.q.dropped)
    assert r.played == ["болтовня", "почти важное"]         # прерванный не возвращается в очередь
    assert all(c.text != "болтовня" for c in r.q.items)


async def test_no_interrupt_when_current_is_close_in_priority():
    r = Rig()
    r.add("важная", 80, source="attention:a1")
    await r.pump()
    r.add("срочная", 90, source="attention:a2")             # 90 - 80 < 15
    assert await r.pump() is None and r.speech.stopped == []
    r.speech.speaking = False
    assert (await r.pump()).text == "срочная"               # дождалась конца


async def test_interrupt_margin_is_a_parameter():
    r = Rig()
    r.add("средняя", 70, source="attention:a1")
    await r.pump()
    r.add("срочная", 90, source="attention:a2")
    cand = await r.q.pump(margin=20)                         # 90 - 70 >= 20
    await r.q.idle()
    assert cand.text == "срочная"


async def test_d3_candidate_interrupts_only_chatter():
    r = Rig()
    r.add("болтовня", 30)                                    # приоритет ниже 35: «болтовня»
    await r.pump()
    r.add("скоро", 60, source="attention:a1")
    assert (await r.pump()).text == "скоро"                  # D3 перебила болтовню
    r2 = Rig()
    r2.add("не болтовня", 36, source="attention:a1")
    await r2.pump()
    r2.add("скоро", 70, source="attention:a2")
    assert await r2.pump() is None                           # 36 не болтовня: D3 ждёт


async def test_interrupt_cancels_reply_that_is_still_being_prepared():
    r = Rig()
    r.hold = asyncio.Event()
    r.add("готовится долго", 40, source="attention:a1")
    await r.q.pump()
    r.add("срочное", 90, source="attention:a2")
    r.hold = None
    cand = await r.q.pump()
    await r.q.idle()
    assert cand.text == "срочное" and r.played == ["срочное"]
    assert any(why == "interrupted" for _, _, why in r.q.dropped)


async def test_stop_all_clears_below_85_and_stops_speech():
    r = Rig()
    r.add("идёт", 50, source="attention:a1")
    await r.pump()
    r.add("в очереди 1", 40, source="attention:a2")
    r.add("в очереди 2", 84, source="attention:a3")
    keep = r.add("срочное", 85, source="attention:a4")
    removed = await r.q.stop_all()
    assert removed == 2 and r.q.items == [keep]
    assert r.speech.stopped[-1] is None                      # SpeechOutput.stop(None)
    assert not r.speech.speaking


async def test_done_future_resolves_spoken_or_dropped():
    r = Rig()
    spoken = r.add("скажу", 50, source="attention:a1")
    gone = r.add("устареет", 20, source="attention:a2", ttl=1)
    await r.pump()
    assert spoken.done.result() is True
    r.clock.t += 5
    r.q.expire()
    assert gone.done.result() is False
