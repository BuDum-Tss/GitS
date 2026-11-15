"""Ответ в канал модуля не дублируется: если единый агент уже отправил сообщение инструментом `<модуль>.message` в тот же адресат,
его итоговая фраза («Ответила: ...») в канал не идёт, но остаётся в диалоге и журнале. Настоящие Companion и ResidentAgent,
среда исполнения и инструмент модуля заглушены (как pi, инструмент зовётся из отдельной задачи, а не из задачи хода)."""
import asyncio

from interfaces.contracts import EndEvent, TextEvent
from services.tracing import events_log
from services.conversation.companion import Companion
from services.conversation.conversation_io import Channel, ConversationIO, ModuleReplyGuard
from services.conversation.resident import ResidentAgent
from services.tools.router import ToolOutcome


class ScriptedRuntime:
    """Среда исполнения: перед текстами вызывает call_tool по сценарию `calls` из отдельной задачи; `gate` держит ход."""

    def __init__(self, calls, texts, gate: asyncio.Event | None = None):
        self.calls, self.texts, self.gate = calls, texts, gate
        self.can_steer = False
        self.tools = []

    async def start(self, system, tools):
        self.tools = tools

    async def run(self, prompt):
        handler = next(t for t in self.tools if t.name == "call_tool").handler
        for tool_id, args in self.calls:
            await asyncio.create_task(handler({"tool_id": tool_id, "args": args}))     # как pi: не из контекста хода
        if self.gate is not None:
            await self.gate.wait()
        for text in self.texts:
            yield TextEvent(text)
        yield EndEvent("ok")

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


def make(tmp_path, calls, texts, fail=(), gate=None, delivered=()):
    c = Companion(str(tmp_path / "memory"), rel=None, speech=Speech())
    c.io = FakeIO()
    c.router = FakeRouter(delivered, fail)
    c.resident = ResidentAgent(c, runtime=ScriptedRuntime(calls, texts, gate))
    c._handle_text = lambda text: c.resident.handle_user(text, use_memory=False)
    return c


def dialogue(c):
    return [m["text"] for m in c._mem.recent_dialogue(20) if m["role"] != "user"]


TG = {"module": "discord", "target": "channel:7"}


async def test_delivery_to_the_reply_address_mutes_even_when_to_is_a_name(tmp_path):
    """Как на скриншоте: to по названию сервера и канала, адрес обращения channel:7; модуль вернул delivered_to=[channel:7]."""
    c = make(tmp_path, [("discord.message", {"to": "Aperture Science / #общее", "text": "Пожалуйста"})], ["Ответила, всё готово"],
             delivered=["channel:7"])
    await ConversationIO(c).from_module("привет", {"module": "discord", "target": "channel:7"})
    assert c.io.delivered == []                                       # второго сообщения в канале нет
    # в диалоге (чат дашборда) и журнале одна её реплика: отправленный текст; отчёта «Ответила…» нет нигде
    assert dialogue(c) == ["Пожалуйста"]
    says = [e for e in events_log.tail(50, {"say"}) if e["text"] in ("Пожалуйста", "Ответила, всё готово")]
    assert [e["text"] for e in says] == ["Пожалуйста"] and says[0]["data"]["channel"] == "discord:channel:7"
    assert c.speech.spoken == []                                      # и не озвучена


async def test_whole_tail_after_delivery_is_muted_but_other_turns_unaffected(tmp_path):
    """Все реплики хода после доставки глушатся, не только одна; в следующем ходе без доставки ответ идёт как обычно."""
    c = make(tmp_path, [("discord.message", {"to": "x", "text": "Вот"})], ["Ответила", "И ещё скажу", "Готово"], delivered=["channel:7"])
    await ConversationIO(c).from_module("привет", {"module": "discord", "target": "channel:7"})
    assert c.io.delivered == [] and dialogue(c) == ["Вот"]
    c.resident._runtime.calls = []
    await ConversationIO(c).from_module("ещё", {"module": "discord", "target": "channel:7"})
    assert [t for _, t in c.io.delivered] == ["Ответила", "И ещё скажу", "Готово"]


async def test_dashboard_turn_is_not_affected_by_delivery_to_a_channel(tmp_path):
    """Ход из дашборда: инструмент доставил в канал, но канал обращения не модуль: итоговая реплика идёт как раньше."""
    c = make(tmp_path, [("discord.message", {"to": "x", "text": "Вот"})], ["Отправила"], delivered=["channel:7"])
    await c.on_user_message("привет")
    assert dialogue(c) == ["Отправила"] and c.speech.spoken == ["Отправила"]


async def test_delivery_to_another_address_or_none_does_not_mute(tmp_path):
    for n, delivered in enumerate((["channel:99"], [])):
        c = make(tmp_path / str(n), [("discord.message", {"to": "channel", "text": "x"})], ["Вот ответ"], delivered=delivered)
        await ConversationIO(c).from_module("привет", TG)
        assert [t for _, t in c.io.delivered] == ["Вот ответ"], delivered


async def test_without_message_tool_reply_is_delivered_once(tmp_path):
    c = make(tmp_path, [("discord.inbox", {"channel": "x"})], ["Вот ответ"], delivered=["channel:7"])
    await ConversationIO(c).from_module("привет", TG)
    assert c.io.delivered == [(Channel("module", "discord", "channel:7"), "Вот ответ")]    # inbox ничего не доставлял


async def test_failed_tool_does_not_mute(tmp_path):
    c = make(tmp_path, [("discord.message", {"to": "channel", "text": "x"})], ["Не вышло"], fail={"discord.message"},
             delivered=["channel:7"])
    await ConversationIO(c).from_module("привет", TG)
    assert [t for _, t in c.io.delivered] == ["Не вышло"]


async def test_owner_dm_message_mutes_reply_in_owner_dm(tmp_path):
    """message(to=owner) доставил в dm:<id владельца>; обращение пришло из лички владельца: ответ уже там."""
    c = make(tmp_path, [("discord.message", {"to": "owner", "text": "x"})], ["Ответила"], delivered=["dm:5"])
    await ConversationIO(c).from_module("привет", {"module": "discord", "target": "dm:5"})
    assert c.io.delivered == []
    c = make(tmp_path / "other", [("discord.message", {"to": "owner", "text": "x"})], ["Ответила"], delivered=["dm:5"])
    await ConversationIO(c).from_module("привет", TG)                 # обращение из канала, а ушло в личку: ответ в канал остаётся
    assert [t for _, t in c.io.delivered] == ["Ответила"]


def test_guard_compares_addresses_exactly():
    g = ModuleReplyGuard(Channel("module", "telegram", "user:-100#5"))
    g.note_delivered(True, ("user:-100",))
    assert not g.answered
    g.note_delivered(False, ("user:-100#5",))
    assert not g.answered
    g.note_delivered(True, ("user:-100#5",))
    assert g.answered
    assert not ModuleReplyGuard(None).answered


async def test_state_is_per_turn_and_does_not_leak_into_the_next_one(tmp_path):
    c = make(tmp_path, [("discord.message", {"to": "channel", "text": "x"})], ["Первый"], delivered=["channel:7"])
    await ConversationIO(c).from_module("раз", TG)
    c.resident._runtime.calls = []                                   # во втором ходе message не зовётся
    await ConversationIO(c).from_module("два", TG)
    assert [t for _, t in c.io.delivered] == ["Первый"]            # первый ход заглушён, второй (без message) доставлен


async def test_parallel_turns_from_different_channels_are_independent(tmp_path):
    """Два хода одновременно (разные каналы, у одного был message): второй свою защиту не наследует и не отдаёт первому."""
    from services.conversation.conversation_io import current_channel, turn_replies
    c = make(tmp_path, [], [])
    a, b = ModuleReplyGuard(Channel("module", "discord", "channel:1")), ModuleReplyGuard(Channel("module", "discord", "channel:2"))

    async def turn(guard, text, answered):
        current_channel.set(guard.channel)
        turn_replies.set(guard)
        if answered:
            guard.note_delivered(True, ("channel:1",))
        await asyncio.sleep(0.01)
        await c.say(text)

    await asyncio.gather(turn(a, "из первого", True), turn(b, "из второго", False))
    assert c.io.delivered == [(b.channel, "из второго")]
    assert dialogue(c) == ["из второго"]              # хвост первого хода после доставки не показан
