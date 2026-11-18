"""Рендер для агента (раздел 6): строка события, пачка, блок «прервано», сводка."""
from services.attention_v2.event import Event, Reply, Sender
from services.attention_v2.queue import Digest
from services.attention_v2.render import (NAME_MAX, clean_name, fmt_hm, fmt_hms, render_batch, render_event,
                                          render_interrupt_note)
from services.attention_v2.runtime import ActionRecord

TS = 1_700_000_000.0


def ev(**kw):
    base = dict(workspace="discord", kind="discord.message", text="кто идёт играть?", ts=TS, priority=30)
    base.update(kw)
    return Event(**base)


def test_event_line_format_with_reply():
    e = ev(sender=Sender(id="1", name="Вася"), reply=Reply("discord.text", "chan-2"))
    assert render_event(e) == f"[{fmt_hms(TS)}] discord/message Вася: кто идёт играть? {{ответ: discord.text}}"


def test_event_line_owner_bot_and_no_sender():
    owner = ev(sender=Sender(id="2", name="Маша", is_owner=True), kind="discord.voice.utterance", text="«Глэдос?»")
    assert render_event(owner) == f"[{fmt_hms(TS)}] discord/voice.utterance Маша (владелец): «Глэдос?»"
    assert "(бот)" in render_event(ev(sender=Sender(id="3", name="Бот", is_bot=True)))
    timer = ev(workspace="core", kind="core.timer", text="напоминание: проверить сборку")
    assert render_event(timer) == f"[{fmt_hms(TS)}] core/timer напоминание: проверить сборку"


def test_sender_name_sanitized():
    e = ev(sender=Sender(id="1", name='Ва\nся "Крутой"\t«X»\r'))
    line = render_event(e)
    assert "\n" not in line and '"' not in line and "«X»" not in line
    assert "Ва ся Крутой X: " in line


def test_clean_name_limits_and_quotes():
    assert clean_name('a"b\'c`d«e»') == "abcde"
    assert clean_name(None) == ""
    long = clean_name("я" * 500)
    assert len(long) == NAME_MAX and long.endswith("…")
    assert clean_name("  много   пробелов \n ") == "много пробелов"


def test_text_single_line_and_limit_1000():
    e = ev(text="строка1\nстрока2\r\n" + "ы" * 2000)
    line = render_event(e)
    assert "\n" not in line
    body = line.split("discord/message ", 1)[1]
    assert body.startswith("строка1 строка2 ") and len(body) == 1000 and body.endswith("…")


def test_reply_tool_sanitized():
    e = ev(reply=Reply('x.say\n"', "t"))
    assert "{ответ: x.say}" in render_event(e)


def test_interrupted_marker():
    assert render_event(ev(interrupted=True)).startswith("⟲ [")
    assert render_event(ev()).startswith("[")


def test_batch_order_by_priority_then_age_with_digest_and_note():
    low = ev(priority=10, text="низкий", ts=TS)
    mid_old = ev(priority=50, text="средний-старый", ts=TS - 5)
    mid_new = ev(priority=50, text="средний-новый", ts=TS)
    high = ev(priority=100, text="высокий", ts=TS + 1, interrupted=True)
    text = render_batch([low, mid_new, high, mid_old], "(пропущено: 2 x/y; последнее 14:05)", "(прервано: ...)")
    lines = text.splitlines()
    assert lines[0].startswith("(пропущено") and lines[1].startswith("(прервано")
    bodies = [x.split(" ", 3)[-1] if not x.startswith("⟲") else x for x in lines[2:]]
    assert "высокий" in lines[2] and lines[2].startswith("⟲")
    assert "средний-старый" in lines[3] and "средний-новый" in lines[4] and "низкий" in lines[5]
    assert len(bodies) == 4


def test_batch_without_extras_is_just_lines():
    assert render_batch([ev(text="один")]).count("\n") == 0


def test_interrupt_note_done_and_unfinished():
    events = [ev(workspace="discord", kind="discord.voice.utterance", text="слышишь?", priority=100,
                 sender=Sender(id="1", name="Маша"))]
    actions = [ActionRecord("discord.say", "done", True, "Секунду…"),
               ActionRecord("memory.search", "started", False),
               ActionRecord("web.search", "done", False, "нашла"),
               ActionRecord("discord.text", "failed", True)]
    note = render_interrupt_note(events, actions, started_at=TS)
    lines = note.splitlines()
    assert lines[0] == (f"(прервано: ход по событиям ниже начался в {fmt_hms(TS)}; успели: discord.say «Секунду…»; "
                        f"не успели: memory.search; сбой: discord.text)")
    assert "web.search" not in note                                   # чтение не считается «успели»
    assert lines[1].lstrip().startswith("[") and "слышишь?" in lines[1]


def test_interrupt_note_without_actions():
    note = render_interrupt_note([ev()], [])
    assert note.splitlines()[0] == "(прервано: ход по событиям ниже; действий не было)"


def test_digest_render_and_take():
    d = Digest()
    assert d.render() == "" and not d
    for _ in range(3):
        d.add(ev(ts=TS))
    d.add(ev(workspace="rimworld", kind="rimworld.event", ts=TS + 120))
    assert d.render() == f"(пропущено: 3 discord/message, 1 rimworld/event; последнее {fmt_hm(TS + 120)})"
    assert d.total == 4
    assert d.take().startswith("(пропущено") and d.render() == "" and d.total == 0
