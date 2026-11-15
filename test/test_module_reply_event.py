"""Сообщение из канала модуля это событие: оно идёт агенту как событие с needs_reply, ответ делает только его инструмент модуля,
её исходящее сообщение пишется событием `module_out`, диалог и чат дашборда из каналов ничего не получают.

Настоящие Companion, ResidentAgent, Router и журнал событий; среда исполнения и инструмент модуля заглушены (как pi: инструмент
исполняется в отдельной задаче по вызову агента)."""
import asyncio
import re
import uuid

from environments.base import Tool
from interfaces.contracts import EndEvent, TextEvent, ToolEvent
from services.attention.core import Item
from services.attention.items import reply_text
from services.conversation.companion import Companion
from services.conversation.resident import ResidentAgent
from services.memory_service import moments as mom
from services.memory_service.episode_summary import format_events
from services.tools.router import Router
from services.tracing import events_log


def item(text, target="channel:7", mid=555, module="discord", kind="discord.message"):
    return Item(id="a-1", module=module, kind=kind, text=text, origin="external", sender={"id": "d:5", "name": "X", "is_owner": False},
                reply_channel={"module": module, "target": target}, addressed_to_me=True, urgency=4, needs_reply=True, memorable=False,
                created=0.0, expires=None, dedupe_key=None, data={"message_id": mid})


class AnsweringRuntime:
    """Агент по заглушке: прочитал в событии адрес и id сообщения и отвечает вызовом <модуль>.message (как настоящий по тексту события)."""

    def __init__(self, comment=None):
        self.comment, self.prompts, self.tools = comment, [], []
        self.can_steer = False

    async def start(self, system, tools):
        self.tools = tools

    async def run(self, prompt):
        self.prompts.append(prompt)
        handler = next(t for t in self.tools if t.name == "call_tool").handler
        module, target, reply_to = re.search(r"инструмент (\w+)\.message \(to=(\S+?), reply_to=(\d+)", prompt).groups()
        if self.comment:
            yield TextEvent(self.comment)
        args = {"to": target, "reply_to": reply_to, "text": f"ответ в {target} " + uuid.uuid4().hex[:6]}
        yield ToolEvent("call_tool", {"tool_id": f"{module}.message", "args": args})
        await asyncio.create_task(handler({"tool_id": f"{module}.message", "args": args}))
        await asyncio.sleep(0.01)
        yield EndEvent("ok")

    async def abort(self):
        pass

    async def reset(self, forget=True):
        pass


class FakeRegistry:
    def __init__(self, tools):
        self.tools = {t.id: t for t in tools}

    def get(self, tool_id):
        return self.tools.get(tool_id)


class FakeIO:
    def __init__(self):
        self.delivered = []

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


def make(tmp_path, comment=None):
    c = Companion(str(tmp_path / "memory"), rel=None, speech=Speech())
    c.io = FakeIO()
    sent = []

    async def message(args):
        sent.append(args)
        return "отправлено"
    params = {k: {"type": "string", "required": False} for k in ("to", "reply_to", "text")}
    tools = [Tool("discord.message", "discord", "message", "d", params=params, risk="read", call=message, sends=True),
             Tool("telegram.message", "telegram", "message", "d", params=params, risk="read", call=message, sends=True)]
    c.router = Router(FakeRegistry(tools), None)
    c.resident = ResidentAgent(c, runtime=AnsweringRuntime(comment))
    return c, sent


def out_rows(*needles):
    return [r for r in events_log.after(0, 5000, {"module_out"}) if any(n in r["text"] for n in needles) or not needles]


def test_reply_text_carries_what_is_needed_to_answer_and_is_the_same_for_everyone():
    owner = item("Сообщение от А в канале: «привет»")
    stranger = item("Сообщение от Б в канале: «привет»")
    owner.sender = {"id": "d:1", "name": "А", "is_owner": True}
    a, b = reply_text(owner).split("\n"), reply_text(stranger).split("\n")
    assert a[0] == owner.text and a[1] == b[1]                        # хвост одинаков: инструмент, адрес, id сообщения
    assert "discord.message (to=channel:7, reply_to=555, text)" in a[1]
    tg = reply_text(item("m", target="user:-100#5", mid=9, module="telegram", kind="telegram.group"))
    assert "telegram.message (to=user:-100#5, reply_to=9" in tg
    assert reply_text(Item(**{**item("x").__dict__, "reply_channel": None})) == "x"      # ответить некуда: только текст


async def test_event_from_channel_is_answered_by_the_tool_and_becomes_module_out(tmp_path):
    c, sent = make(tmp_path)
    ev = item("Сообщение от X в канале: «сколько будет два плюс два»")
    await c.resident.handle_event(ev.kind, reply_text(ev), ev.origin)
    assert [a["to"] for a in sent] == ["channel:7"] and sent[0]["reply_to"] == "555"
    outs = out_rows("ответ в channel:7")
    assert len(outs) == 1 and outs[0]["text"] == sent[0]["text"]
    assert outs[0]["data"]["module"] == "discord" and outs[0]["data"]["own"] is True and outs[0]["data"]["to"] == "channel:7"
    assert c.io.delivered == []                                       # реплика в канал автоматически не уходит
    assert c._mem.recent_dialogue(10) == []                           # диалог (и чат дашборда) из канала ничего не получил
    assert not [r for r in events_log.after(0, 5000, {"user"}) if "сколько будет" in r["text"]]


async def test_agent_speech_during_the_event_never_goes_to_the_channel(tmp_path):
    """Её слова в ходе по событию идут как при любом событии (речь, чат по правилам внимания), но не в канал."""
    c, sent = make(tmp_path, comment="Сейчас отвечу")
    ev = item("Сообщение от X: «привет»")
    await c.resident.handle_event(ev.kind, reply_text(ev), ev.origin)
    assert c.io.delivered == [] and len(sent) == 1
    assert c.speech.spoken == ["Сейчас отвечу"]


async def test_two_messages_from_different_channels_are_handled_in_turn_without_mixing(tmp_path):
    c, sent = make(tmp_path)
    a = item("Сообщение от А: «раз»", target="channel:1", mid=11)
    b = item("Сообщение от Б: «два»", target="channel:2", mid=22, module="telegram", kind="telegram.group")
    await asyncio.gather(c.resident.handle_event(a.kind, reply_text(a), a.origin),
                         c.resident.handle_event(b.kind, reply_text(b), b.origin))
    prompts = c.resident._runtime.prompts
    assert len(prompts) == 2 and "раз" in prompts[0] and "два" in prompts[1] and "два" not in prompts[0]
    assert [(x["to"], x["reply_to"]) for x in sent] == [("channel:1", "11"), ("channel:2", "22")]
    assert c.io.delivered == []


async def test_failed_send_is_not_an_outgoing_event(tmp_path):
    c, _ = make(tmp_path)
    async def broken(args):
        raise RuntimeError("сбой")
    bad = Tool("discord.message", "discord", "message", "d", params=c.router.registry.tools["discord.message"].params,
               risk="read", sends=True, call=broken)
    c.router.registry.tools["discord.message"] = bad
    ev = item("Сообщение от X: «привет»")
    await c.resident.handle_event(ev.kind, reply_text(ev), ev.origin)
    assert out_rows("ответ в channel:7") == []


def test_episode_chain_sees_incoming_event_and_her_outgoing_message():
    """Момент: входящее (module_event discord.*) пик, её исходящее module_out входит в контекст и в текст для эпизода."""
    mark = uuid.uuid4().hex[:6]
    events_log.log_nowait("module_event", f"Сообщение от X в канале: «вопрос {mark}»", module="discord", event_kind="discord.message",
                          sender="X")
    events_log.log_nowait("module_out", f"ответ {mark}", module="discord", to="channel:7", own=True)
    events_log.flush()
    rows = [r for r in events_log.after(0, 5000) if mark in r["text"]]
    assert [r["kind"] for r in rows] == ["module_event", "module_out"]
    (m,) = mom.select_moments(rows, mom.MomentCfg(), rows[-1]["ts"] + 5000)
    assert [e.kind for e in m.events] == ["module_event", "module_out"]
    text = format_events([{"id": e.id, "ts": e.ts, "kind": e.kind, "text": e.text} for e in m.events])
    assert "она написала в чат: ответ" in text
    from services.memory_service.live_episodes import CONTEXT_KINDS
    assert "module_out" in CONTEXT_KINDS and "module_out" in mom.CONTEXT_KINDS
