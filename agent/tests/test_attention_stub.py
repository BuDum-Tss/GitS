"""Заглушка менеджера внимания (ТЗ 02, CORE-29): событие, речь, вопрос, задача."""
import asyncio
import time

from hub_helpers import FakeCompanion, FakeIO

import approvals
import events_log
from attention import Attention, Question


class Graph:
    def __init__(self):
        self.items = []

    def enqueue(self, speaker, text):
        self.items.append((speaker, text))


def make():
    c, io = FakeCompanion(), FakeIO()
    return Attention(c, io), c, io


async def settle():
    await asyncio.sleep(0.05)


async def test_event_is_logged_and_owner_address_becomes_user_input():
    att, c, io = make()
    await att.submit("tg", {"kind": "tg.dm", "text": "Ты скоро?", "addressed_to_me": True,
                            "sender": {"id": "tg:1", "name": "Аня", "is_owner": True},
                            "reply_channel": {"module": "tg", "target": "tg:1"}})
    await settle()
    assert io.inputs == [("Ты скоро?", {"module": "tg", "target": "tg:1"}, {"id": "tg:1", "name": "Аня", "is_owner": True})]
    assert events_log.tail(5, {"module_event"})[-1]["data"]["event_kind"] == "tg.dm"


async def test_event_without_address_owner_or_channel_is_only_logged():
    att, c, io = make()
    for ev in ({"kind": "tg.dm", "text": "а", "addressed_to_me": True, "sender": {"id": "x", "is_owner": False},
                "reply_channel": {"module": "tg", "target": "x"}},
               {"kind": "tg.dm", "text": "б", "addressed_to_me": False, "sender": {"id": "x", "is_owner": True},
                "reply_channel": {"module": "tg", "target": "x"}},
               {"kind": "tg.dm", "text": "в", "addressed_to_me": True, "sender": {"id": "x", "is_owner": True}},
               {"kind": "tg.news", "text": "г"}):
        await att.submit("tg", ev)
    await settle()
    assert io.inputs == [] and c.about == [] and c.said == []                     # не озвучивает
    assert len(events_log.tail(10, {"module_event"})) == 4


async def test_memorable_event_goes_to_graph_queue():
    att, c, io = make()
    c.graph = Graph()
    await att.submit("tg", {"kind": "tg.dm", "text": "Маша переезжает в Казань", "memorable": True,
                            "sender": {"id": "tg:2", "name": "Маша"}})
    await att.submit("tg", {"kind": "tg.dm", "text": "Ерунда"})
    assert c.graph.items == [("Маша", "Маша переезжает в Казань")]


async def test_verbatim_speech_waits_for_silence():
    att, c, io = make()
    await att.submit_speech("rimworld", {"text": "Борис снова уснул.", "mode": "verbatim", "emotion": "angry"})
    assert c.said == [("Борис снова уснул.", "angry")]
    c.speaking = True
    await att.submit_speech("rimworld", {"text": "Вторая", "mode": "verbatim"})
    c.speaking = False
    c._last_user_ts = time.time() - 2                                              # пользователь говорил 2 с назад
    await att.submit_speech("rimworld", {"text": "Третья", "mode": "verbatim"})
    assert len(c.said) == 1
    c._last_user_ts = time.time() - 6
    await att.submit_speech("rimworld", {"text": "Четвёртая", "mode": "verbatim"})
    assert [s[0] for s in c.said] == ["Борис снова уснул.", "Четвёртая"]


async def test_fact_speech_goes_to_speak_about():
    att, c, io = make()
    await att.submit_speech("rimworld", {"text": "Начался набег.", "mode": "fact"})
    await att.submit_speech("rimworld", {"text": "  "})                            # пустое игнорируется
    await settle()
    assert c.about == [("Начался набег.", False, False)] and c.said == []


async def test_job_done_and_failed_become_facts_once_but_waiting_user_does_not():
    att, c, io = make()
    base = {"job_id": "j-00000001", "title": "Рендер"}
    await att.on_job_update("montage", {**base, "state": "running", "stage": "a"})
    await att.on_job_update("montage", {**base, "state": "waiting_user", "question": {"question_id": "q"}})
    await att.on_job_update("montage", {**base, "state": "done", "text": "Готово: 48 секунд."})
    await att.on_job_update("montage", {**base, "state": "done"})                  # повтор той же смены не дублируется
    await att.on_job_update("montage", {"job_id": "j-00000002", "state": "failed", "error": {"code": "internal", "message": "нет места"}})
    await settle()
    assert len(c.about) == 2
    assert "Рендер" in c.about[0][0] and "выполнена" in c.about[0][0] and "48 секунд" in c.about[0][0]
    assert "не удалась" in c.about[1][0] and "нет места" in c.about[1][0]


async def test_submit_question_asks_via_approvals_with_fact_announcement():
    att, c, io = make()
    task = asyncio.create_task(att.submit_question(Question("choice", "Утвердить план?", ["да, утвердить", "переделать"],
                                                           timeout_s=30, source="montage")))
    for _ in range(200):
        if approvals.has_pending():
            break
        await asyncio.sleep(0.005)
    assert approvals.pending_kind() == "choice"
    fact, must, question = c.about[-1]
    assert must and question and "montage" in fact and "Утвердить план?" in fact and "переделать" in fact
    assert await approvals.try_answer("переделать")
    assert await task == ("переделать", "user")
