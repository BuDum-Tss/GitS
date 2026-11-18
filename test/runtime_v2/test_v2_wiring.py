"""Подключение внимания v2 к ядру под флагом: маршрут в AttentionEventSink, чат дашборда, /api/attention_v2, признак read_only."""
import asyncio
import json
from types import SimpleNamespace

from gits_sdk import protocol as P

from http_api import handle_api_attention_v2, set_attention_v2
from services.attention import AttentionEventSink
from services.attention_v2.event import USER
from services.attention_v2.kinds import KindRegistry
from services.attention_v2.routing import (DASHBOARD_KIND, DEFAULT_PREFIXES, V2Router, dashboard_event, kind_prefixes,
                                           register_core_kinds, v2_enabled)
from services.attention_v2.testing import Rig
from services.conversation.service import ConversationService


class LegacyAttention:
    def __init__(self):
        self.submitted = []

    async def submit(self, peer_id, event):
        self.submitted.append((peer_id, event))


class FakeV2:
    """Внимание v2 для маршрута: запоминает принятые события."""

    def __init__(self):
        self.events = []

    async def process_event(self, e):
        self.events.append(e)


def _router(manifests=None):
    registry = KindRegistry()
    register_core_kinds(registry)
    att = FakeV2()
    router = V2Router(att, registry, DEFAULT_PREFIXES, manifest_of=lambda peer: (manifests or {}).get(peer))
    return router, att, registry


# ---- флаг и префиксы ----------------------------------------------------------------------------------------------------

def test_flag_is_off_by_default_and_prefixes_are_configurable():
    assert v2_enabled({}) is False and v2_enabled({"GITS_ATTENTION": "v1"}) is False and v2_enabled({"GITS_ATTENTION": "V2"}) is True
    assert kind_prefixes({}) == ("discord.", "telegram.", "voicechat.", "dashboard.", "rimworld.", "core.timer")
    assert kind_prefixes({"GITS_ATTENTION_V2_KINDS": " discord. , rimworld.alert "}) == ("discord.", "rimworld.alert")


# ---- AttentionEventSink --------------------------------------------------------------------------------------------------

async def test_sink_without_v2_sends_everything_to_the_old_attention():
    legacy, recorded = LegacyAttention(), []
    sink = AttentionEventSink(legacy, recorder=lambda peer, ev: recorded.append(peer))
    await sink.submit("discord", {"kind": "discord.message", "text": "привет"})
    await sink.submit("rimworld", {"kind": "rimworld.raid", "text": "рейд"})
    assert [p for p, _ in legacy.submitted] == ["discord", "rimworld"] and recorded == ["discord", "rimworld"]


async def test_sink_with_v2_routes_by_kind_prefix():
    legacy = LegacyAttention()
    router, v2, _ = _router()
    recorded = []
    sink = AttentionEventSink(legacy, recorder=lambda peer, ev: recorded.append(peer), v2=router)
    await sink.submit("discord", {"kind": "discord.message", "text": "привет", "urgency_hint": 3,
                                  "sender": {"id": "u1", "name": "Вася"}})
    await sink.submit("montage", {"kind": "montage.done", "text": "готово", "urgency_hint": 3})       # префикса нет: старое внимание
    await sink.submit("telegram", {"kind": "telegram.dm", "text": "привет"})
    assert [e.kind for e in v2.events] == ["discord.message", "telegram.dm"]
    assert v2.events[0].workspace == "discord" and v2.events[0].priority == 50 and v2.events[0].sender.name == "Вася"
    assert [(p, e["kind"]) for p, e in legacy.submitted] == [("montage", "montage.done")]
    assert recorded == ["discord", "montage", "telegram"]                    # журнал модулей ведётся как раньше


async def test_router_registers_event_kinds_from_manifest_and_clamps_by_registry():
    kinds = [{"kind": "discord.message", "default_priority": "NORMAL", "max_priority": "IMPORTANT", "ttl_s": 900, "coalesce": True,
              "reply_tool": "discord.text"}]
    router, v2, registry = _router({"discord": {"event_kinds": kinds}})
    await router.submit("discord", {"kind": "discord.message", "text": "x", "urgency_hint": 5})
    spec = registry.get("discord.message")
    assert spec.legacy is False and spec.max_priority == 70 and spec.reply_tool == "discord.text"
    assert v2.events[0].kind == "discord.message"


async def test_router_undeclared_kind_works_as_legacy():
    router, v2, registry = _router()
    await router.submit("telegram", {"kind": "telegram.dm", "text": "x"})
    assert registry.get("telegram.dm").legacy is True and v2.events


# ---- чат дашборда --------------------------------------------------------------------------------------------------------

def test_dashboard_event_is_user_priority_with_group_and_reply():
    registry = KindRegistry()
    register_core_kinds(registry)
    e = dashboard_event("привет", ts=1000.0)
    assert registry.normalize(e).accepted
    assert (e.workspace, e.kind, e.priority) == ("dashboard", DASHBOARD_KIND, USER)
    assert e.sender.is_owner and e.group_key == "dashboard:owner" and e.reply.tool == "dashboard.text"
    assert e.expires_at == 1000.0 + 15 * 60


def _host(v2):
    logged = []
    host = SimpleNamespace(
        _log=SimpleNamespace(log_nowait=lambda *a, **k: logged.append(a)), _pending_memory=0, _last_user_ts=0.0,
        presence=SimpleNamespace(update=lambda **k: None), attention=None, attention_v2=v2,
        _approver=SimpleNamespace(has_pending=lambda: False), _check_correction=lambda text: None,
        arm_idle_consolidation=lambda: None, resident=SimpleNamespace(handle_user=_must_not_be_called),
        _busy=asyncio.Lock())
    return host


async def _must_not_be_called(*a, **k):
    raise AssertionError("при v2 сообщение идёт событием, а не прямым вызовом агента")


async def test_dashboard_message_becomes_user_event_under_v2():
    v2 = FakeV2()
    service = ConversationService(_host(v2))
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    await service._handle("привет, ты тут?", None, done, 0, [])
    assert len(v2.events) == 1
    e = v2.events[0]
    assert e.kind == "dashboard.message" and e.text == "привет, ты тут?" and e.priority == USER


async def test_dashboard_message_goes_to_resident_when_v2_is_off():
    calls = []

    async def handle_user(text, use_memory=True):
        calls.append(text)

    host = _host(None)
    host.resident = SimpleNamespace(handle_user=handle_user)
    done = asyncio.get_running_loop().create_future()
    done.set_result(None)
    await ConversationService(host)._handle("привет", None, done, 0, [])
    assert calls == ["привет"]


# ---- /api/attention_v2 ----------------------------------------------------------------------------------------------------

async def test_api_attention_v2_disabled_and_enabled():
    set_attention_v2(None)
    resp = await handle_api_attention_v2(SimpleNamespace(query={}))
    assert json.loads(resp.text) == {"enabled": False}
    r = Rig()
    await r.send(20, text="тихое")
    set_attention_v2(r.attention)
    try:
        body = json.loads((await handle_api_attention_v2(SimpleNamespace(query={"limit": "5"}))).text)
    finally:
        set_attention_v2(None)
    assert body["enabled"] is True and body["status"]["queue"]["size"] == 1
    assert body["log"] and {x["op"] for x in body["log"]} <= {"new", "state", "note"} and "data" not in body["log"][0]


# ---- признак read_only в манифесте ---------------------------------------------------------------------------------------

def _tool_dict(**extra):
    return {"name": "inbox", "description": "читать", "params": {}, "risk": "auto", "availability": "always", **extra}


def test_tool_spec_read_only_defaults_to_false_and_roundtrips():
    old = P.ToolSpec.from_dict(_tool_dict())                      # манифест без поля читается как раньше
    assert old.read_only is False and "read_only" not in old.to_dict()
    new = P.ToolSpec.from_dict(_tool_dict(read_only=True))
    assert new.read_only is True and new.to_dict()["read_only"] is True


def test_manifest_validation_rejects_non_bool_read_only():
    manifest = {"id": "x", "role": "workspace", "ownership": "owned", "title": "X", "description": "d", "version": "1.0.0",
                "tools": [_tool_dict(read_only="да")]}
    assert any("read_only" in p for p in P.validate_manifest(manifest))
    manifest["tools"] = [_tool_dict(read_only=True)]
    assert not any("read_only" in p for p in P.validate_manifest(manifest))
