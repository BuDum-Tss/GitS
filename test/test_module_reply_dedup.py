"""Реплики хода из канала модуля копятся до конца хода: комментарии до инструмента не уходят никуда, ответ после последнего
инструмента доставляется один раз, ход с доставкой инструментом в адрес обращения не добавляет ничего (его текст записан как её
реплика), оборванный ход молчит. Настоящие Companion и ResidentAgent; среда исполнения и инструмент модуля заглушены
(как pi: ToolEvent приходит в потоке событий, а инструмент исполняется в отдельной задаче, не в задаче хода)."""
import asyncio

import pytest

from interfaces.contracts import EndEvent, TextEvent, ToolEvent
from services.conversation.companion import Companion
from services.conversation.conversation_io import Channel, ConversationIO, ModuleReplyGuard, current_channel, turn_replies
from services.conversation.resident import ResidentAgent
from services.tools.router import ToolOutcome
from services.tracing import events_log


class ScriptedRuntime:
    """Ход по сценарию шагов: ("text", t), ("tool", id, args), ("raise",), ("end", status), ("wait",) - ждать `gate`."""

    def __init__(self, steps, gate: asyncio.Event | None = None):
        self.steps, self.gate = list(steps), gate
        self.can_steer = False
        self.tools = []

    async def start(self, system, tools):
        self.tools = tools

    async def run(self, prompt):
        handler = next(t for t in self.tools if t.name == "call_tool").handler
        status = "ok"
        for step in self.steps:
            if step[0] == "text":
                yield TextEvent(step[1])
            elif step[0] == "tool":
                yield ToolEvent("call_tool", {"tool_id": step[1], "args": step[2]})
                await asyncio.create_task(handler({"tool_id": step[1], "args": step[2]}))
            elif step[0] == "raise":
                raise RuntimeError("сбой среды")
            elif step[0] == "end":
                status = step[1]
            elif step[0] == "wait":
                await self.gate.wait()
        yield EndEvent(status)

    async def abort(self):
        pass

    async def reset(self, forget=True):
        pass


class FakeRouter:
    """Роутер с типизированным исходом: `delivered` - адреса, куда инструмент message доставил сообщение (как у окружений)."""

    def __init__(self, delivered=(), fail=()):
        self.calls, self.delivered, self.fail = [], tuple(delivered), set(fail)
        self.registry = None

    async def run_tool_outcome(self, tool_id, args):
        self.calls.append((tool_id, args))
        if tool_id in self.fail:
            return ToolOutcome(False, "нельзя", "denied")
        return ToolOutcome(True, "отправлено", None, self.delivered if tool_id.endswith(".message") else ())


class FakeIO:
    def __init__(self):
        self.delivered: list[tuple[Channel, str]] = []

    async def deliver(self, channel, text):
        self.delivered.append((channel, text))
        return True


class Speech:
    def __init__(self):
        self.spoken = []

    async def speak(self, utterance_id, text, emotion, priority):
        self.spoken.append(text)

    async def stop(self, utterance_id=None):
        pass

    def is_speaking(self):
        return False

    def current_segment(self):
        return ""


def make(tmp_path, steps, fail=(), gate=None, delivered=()):
    c = Companion(str(tmp_path / "memory"), rel=None, speech=Speech())
    c.io = FakeIO()
    c.router = FakeRouter(delivered, fail)
    c.resident = ResidentAgent(c, runtime=ScriptedRuntime(steps, gate))
    c._handle_text = lambda text: c.resident.handle_user(text, use_memory=False)
    return c


def dialogue(c):
    return [m["text"] for m in c._mem.recent_dialogue(20) if m["role"] != "user"]


def sent(c):
    return [t for _, t in c.io.delivered]


TG = {"module": "discord", "target": "channel:7"}
MSG = ("tool", "discord.message", {"to": "Aperture Science / #общее", "text": "Пожалуйста"})


async def ask(c, reply_channel=TG):
    await ConversationIO(c).from_module("привет", reply_channel)


async def test_delivery_to_the_reply_address_leaves_one_reply_and_no_report(tmp_path):
    """Как на скриншоте: to по названию, адрес обращения channel:7, модуль вернул delivered_to=[channel:7]."""
    c = make(tmp_path, [("text", "Сейчас напишу"), MSG, ("text", "Ответила, всё готово")], delivered=["channel:7"])
    await ask(c)
    assert c.io.delivered == []                                       # ни комментария, ни отчёта в канале
    assert dialogue(c) == ["Пожалуйста"]                              # в диалоге одна её реплика: отправленный текст
    says = [e for e in events_log.tail(50, {"say"}) if e["text"] in ("Пожалуйста", "Сейчас напишу", "Ответила, всё готово")]
    assert [e["text"] for e in says] == ["Пожалуйста"] and says[0]["data"]["channel"] == "discord:channel:7"
    assert c.speech.spoken == []                                      # и ничего не озвучено


async def test_comment_before_tool_is_dropped_and_reply_after_last_tool_is_delivered_once(tmp_path):
    c = make(tmp_path, [("text", "Сейчас посмотрю"), ("tool", "discord.inbox", {}), ("text", "Вот что нашла")])
    await ask(c)
    assert sent(c) == ["Вот что нашла"] and dialogue(c) == ["Вот что нашла"]


async def test_two_tool_calls_in_a_row_and_comment_between_are_dropped(tmp_path):
    c = make(tmp_path, [("text", "Раз"), ("tool", "a.one", {}), ("text", "Между вызовами"), ("tool", "a.two", {}), ("text", "Итог")])
    await ask(c)
    assert sent(c) == ["Итог"] and dialogue(c) == ["Итог"]


async def test_turn_without_tools_delivers_every_reply_in_order(tmp_path):
    c = make(tmp_path, [("text", "Раз"), ("text", "Два")])
    await ask(c)
    assert sent(c) == ["Раз", "Два"] and dialogue(c) == ["Раз", "Два"] and c.speech.spoken == []


async def test_delivery_to_another_address_or_none_keeps_the_reply(tmp_path):
    for n, delivered in enumerate((["channel:99"], [])):
        c = make(tmp_path / str(n), [("tool", "discord.message", {"to": "x", "text": "в другой"}), ("text", "Вот ответ")],
                 delivered=delivered)
        await ask(c)
        assert sent(c) == ["Вот ответ"], delivered


async def test_failed_delivery_keeps_the_reply(tmp_path):
    c = make(tmp_path, [MSG, ("text", "Не вышло")], fail={"discord.message"}, delivered=["channel:7"])
    await ask(c)
    assert sent(c) == ["Не вышло"]


async def test_owner_dm_delivery_counts_only_for_the_owner_dm_address(tmp_path):
    steps = [("tool", "discord.message", {"to": "owner", "text": "тебе"}), ("text", "Ответила")]
    c = make(tmp_path, steps, delivered=["dm:5"])
    await ask(c, {"module": "discord", "target": "dm:5"})
    assert c.io.delivered == [] and dialogue(c) == ["тебе"]
    c = make(tmp_path / "other", steps, delivered=["dm:5"])
    await ask(c)                                                      # обращение из канала, ушло в личку: ответ в канал остаётся
    assert sent(c) == ["Ответила"]


@pytest.mark.parametrize("steps", [[("text", "Скажу"), ("raise",)], [("text", "Скажу"), ("end", "error")],
                                   [("text", "Скажу"), ("end", "aborted")]])
async def test_broken_turn_delivers_nothing(tmp_path, steps):
    c = make(tmp_path, steps)
    await ask(c)
    assert c.io.delivered == [] and dialogue(c) == []


async def test_cancelled_turn_delivers_nothing(tmp_path):
    gate = asyncio.Event()
    c = make(tmp_path, [("text", "Скажу"), ("wait",)], gate=gate)
    task = asyncio.create_task(ask(c))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert c.io.delivered == [] and dialogue(c) == []


async def test_dashboard_turn_is_not_touched(tmp_path):
    """Ход из дашборда: реплики идут в речь сразу, комментарий до инструмента тоже (там ничего не копится)."""
    c = make(tmp_path, [("text", "Сейчас"), MSG, ("text", "Отправила")], delivered=["channel:7"])
    await c.on_user_message("привет")
    assert c.speech.spoken == ["Сейчас", "Отправила"] and dialogue(c) == ["Сейчас", "Отправила"] and c.io.delivered == []


async def test_state_is_per_turn_and_does_not_leak_into_the_next_one(tmp_path):
    c = make(tmp_path, [MSG, ("text", "Первый")], delivered=["channel:7"])
    await ask(c)
    c.resident._runtime.steps = [("text", "Второй")]
    await ask(c)
    assert sent(c) == ["Второй"]                                       # первый ход ответил инструментом, второй обычным текстом


async def test_parallel_turns_from_different_channels_are_independent(tmp_path):
    """Два хода одновременно (разные каналы): у первого была доставка в свой адрес, у второго нет; каждый решает за себя."""
    c = make(tmp_path, [])
    a, b = ModuleReplyGuard(Channel("module", "discord", "channel:1")), ModuleReplyGuard(Channel("module", "discord", "channel:2"))

    async def turn(guard, text, delivered):
        current_channel.set(guard.channel)
        turn_replies.set(guard)
        await c.say(text)
        await asyncio.sleep(0.01)
        if delivered:
            guard.note_delivered(True, ("channel:1",))
        await c.release_channel_replies(guard, True)

    await asyncio.gather(turn(a, "из первого", True), turn(b, "из второго", False))
    assert c.io.delivered == [(b.channel, "из второго")] and dialogue(c) == ["из второго"]


def test_guard_compares_addresses_exactly():
    g = ModuleReplyGuard(Channel("module", "telegram", "user:-100#5"))
    assert not g.note_delivered(True, ("user:-100",)) and not g.note_delivered(False, ("user:-100#5",))
    assert not g.answered
    assert g.note_delivered(True, ("user:-100#5",)) and g.answered
    assert not ModuleReplyGuard(None).note_delivered(True, ("x",))
