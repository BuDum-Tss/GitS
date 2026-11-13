"""Модуль Twitch против FakeCore: события чата и канала, инструменты, доставка, токены, EventSub. Сеть Twitch подменена."""
import asyncio
import time

import pytest
from gits_sdk import ModuleClient, ToolError
from gits_sdk.testing import FakeCore

from eventsub import EventSubSession
from manifest import build_manifest
from twitch_api import Helix, Tokens, TwitchError
from twitch_module import TwitchModule

CHANNEL_ID, BOT_ID = "100", "200"


def token(login, uid):
    return {"access": f"acc-{login}", "refresh": f"ref-{login}", "expires_at": time.time() + 3600, "user_id": uid, "login": login}


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, configured=True, broadcaster=True, terms=("Анна",), module_settings=None):
        self.mem = tmp_path / "memory"
        self.calls: list[tuple] = []
        self.responses: dict[tuple, tuple] = {}
        self.terms = list(terms)
        if configured:
            monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
            monkeypatch.setenv("TWITCH_CLIENT_SECRET", "SECRET-VALUE")
            monkeypatch.setenv("TWITCH_CHANNEL", "streamer")
            tokens = Tokens(str(self.mem))
            tokens.put("bot", token("glados", BOT_ID))
            if broadcaster:
                tokens.put("broadcaster", token("streamer", CHANNEL_ID))
        self.core = FakeCore(settings={"quiet_until": 0.0, "dnd": False}, module_settings=module_settings)

    async def __aenter__(self):
        await self.core.start()
        rig = self

        @self.core.on_request("core.privacy_terms")
        def privacy(body):
            return {"terms": rig.terms}

        self.client = ModuleClient(build_manifest(), url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.mod = TwitchModule(self.client, str(self.mem))

        async def request(method, url, *, headers=None, params=None, json_body=None, data=None):
            rig.calls.append((method, url.split("/helix")[-1], params, json_body, headers))
            key = (method, url.split("/helix")[-1])
            if key in rig.responses:
                return rig.responses[key]
            if key == ("GET", "/users"):
                return 200, {"data": [{"id": CHANNEL_ID, "login": "streamer"}]}
            if key == ("POST", "/chat/messages"):
                return 200, {"data": [{"message_id": "m1", "is_sent": True}]}
            return 200, {"data": []}

        self.mod.helix._request = request
        self._task = asyncio.create_task(self.client.run())
        await self.core.wait_connected()
        await self.client.wait_connected(5)
        await self.mod.privacy.refresh()
        return self

    async def __aexit__(self, *exc):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        await self.core.stop()

    def sent(self, path):
        return [c for c in self.calls if c[1] == path]


def chat_ev(text="привет", login="viewer", uid="300", mid="msg1", **extra):
    return {"chatter_user_login": login, "chatter_user_name": login.title(), "chatter_user_id": uid, "message_id": mid,
            "message": {"text": text}, **extra}


async def test_chat_message_becomes_event(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert await rig.mod.on_event("channel.chat.message", chat_ev("как играется?"))
        ev = (await rig.core.wait_message("event")).body
    assert ev["kind"] == "twitch.chat" and ev["text"] == "Сообщение от Viewer в чате Twitch: «как играется?»"
    assert ev["sender"] == {"id": "tw:300", "name": "Viewer", "is_owner": False}
    assert ev["reply_channel"] == {"module": "twitch", "target": "chat:msg1"} and ev["dedupe_key"] == "tw:chat:msg1"
    assert not ev.get("addressed_to_me")


async def test_mention_and_owner_are_addressed(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await rig.mod.on_event("channel.chat.message", chat_ev("@glados ты тут?", mid="a"))
        await rig.mod.on_event("channel.chat.message", chat_ev("привет всем", login="streamer", uid=CHANNEL_ID, mid="b"))
        await rig.core.wait_for(lambda: len(rig.core.events) == 2)
        mention, owner = rig.core.events
    assert mention["kind"] == "twitch.mention" and mention["addressed_to_me"] is True
    assert owner["sender"]["is_owner"] is True and owner["addressed_to_me"] is True


async def test_own_messages_are_ignored_and_chat_is_rate_limited(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, module_settings={"chat_events_per_min": 2}) as rig:
        assert not await rig.mod.on_event("channel.chat.message", chat_ev("я сама", login="glados"))
        results = [await rig.mod.on_event("channel.chat.message", chat_ev(f"м{i}", mid=f"x{i}")) for i in range(4)]
        assert results[-1] is False and results.count(True) == 2
        assert await rig.mod.on_event("channel.chat.message", chat_ev("@glados эй", mid="y"))      # обращение лимит не режет


async def test_channel_events(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        await rig.mod.on_event("channel.follow", {"user_id": "1", "user_name": "Вася", "followed_at": "t"})
        await rig.mod.on_event("channel.raid", {"from_broadcaster_user_name": "Друг", "viewers": 42})
        await rig.mod.on_event("channel.cheer", {"user_name": "Петя", "bits": 100, "message": "держи"})
        await rig.mod.on_event("stream.online", {})
        await rig.core.wait_for(lambda: len(rig.core.events) == 4)
        kinds = [e["kind"] for e in rig.core.events]
        assert kinds == ["twitch.follow", "twitch.raid", "twitch.cheer", "twitch.stream_online"]
        assert "42" in rig.core.events[1]["text"] and rig.mod.live is True
        assert len(rig.mod.recent) == 4


async def test_chat_say_sends_and_splits(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        res = await rig.core.call_tool("chat_say", {"text": "слово " * 200})
        assert res == {"sent": True}
        msgs = rig.sent("/chat/messages")
        assert len(msgs) == 3 and all(len(m[3]["message"]) <= 500 for m in msgs)
        assert msgs[0][3]["sender_id"] == BOT_ID and msgs[0][3]["broadcaster_id"] == CHANNEL_ID


async def test_privacy_blocks_personal_words(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("chat_say", {"text": "Привет, Анна!"})
        assert e.value.code == "denied" and not rig.sent("/chat/messages")


async def test_deliver_replies_to_message(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        assert await rig.mod.deliver({"target": "chat:msg9", "text": "тут"}) == {"delivered": True}
        assert rig.sent("/chat/messages")[0][3]["reply_parent_message_id"] == "msg9"
        with pytest.raises(ToolError):
            await rig.mod.deliver({"target": "tg:1", "text": "x"})


async def test_not_configured_is_honest(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, configured=False) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("chat_say", {"text": "привет"})
        assert e.value.code == "unavailable"
        assert rig.mod.snapshot()[0].startswith("Twitch: не настроен")


async def test_moderation_refuses_owner_and_sends_ban(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch) as rig:
        rig.responses[("GET", "/users")] = 200, {"data": [{"id": "300", "login": "bad"}]}
        out = await rig.core.call_tool("timeout_user", {"user": "@Bad", "seconds": 60, "reason": "спам"})
        assert out["done"] and rig.sent("/moderation/bans")[0][3] == {"data": {"user_id": "300", "reason": "спам", "duration": 60}}
        rig.responses[("GET", "/users")] = 200, {"data": [{"id": CHANNEL_ID, "login": "streamer"}]}
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("ban_user", {"user": "streamer"})
        assert e.value.code == "denied"


async def test_set_stream_needs_broadcaster_login(tmp_path, monkeypatch):
    async with Rig(tmp_path, monkeypatch, broadcaster=False) as rig:
        with pytest.raises(ToolError) as e:
            await rig.core.call_tool("set_stream", {"title": "новый стрим"})
        assert e.value.code == "unavailable"


async def test_token_refresh_on_expiry_and_on_401(tmp_path):
    tokens = Tokens(str(tmp_path))
    tokens.put("bot", {**token("glados", BOT_ID), "expires_at": time.time() - 5})
    api = Helix(tokens, "cid", "SECRET-VALUE")
    seen = []

    async def request(method, url, *, headers=None, params=None, json_body=None, data=None):
        seen.append((url, headers, data))
        if "oauth2/token" in url:
            return 200, {"access_token": f"new{len(seen)}", "refresh_token": "ref2", "expires_in": 3600}
        return (401, {"message": "bad"}) if headers["Authorization"] == "Bearer new1" else (200, {"data": []})

    api._request = request
    await api.call("bot", "GET", "/users")
    assert tokens.get("bot")["refresh"] == "ref2" and tokens.get("bot")["access"] != "acc-glados"
    assert len(seen) == 4                                   # обновление по сроку, 401, обновление, успех
    reloaded = Tokens(str(tmp_path))
    assert reloaded.get("bot")["refresh"] == tokens.get("bot")["refresh"]


async def test_error_text_hides_secrets(tmp_path):
    tokens = Tokens(str(tmp_path))
    tokens.put("bot", token("glados", BOT_ID))
    api = Helix(tokens, "cid", "SECRET-VALUE")

    async def request(*a, **k):
        return 403, {"message": "denied for acc-glados SECRET-VALUE"}

    api._request = request
    with pytest.raises(TwitchError) as e:
        await api.call("bot", "GET", "/users")
    assert "acc-glados" not in str(e.value) and "SECRET-VALUE" not in str(e.value)


async def test_eventsub_welcome_subscribes_and_dispatches():
    created, got = [], []

    class Api:
        async def subscribe(self, role, sub_type, version, condition, session_id):
            created.append((role, sub_type, session_id))
            if sub_type == "channel.cheer":
                raise TwitchError(403, "нет прав")

    async def on_event(kind, ev):
        got.append((kind, ev))

    s = EventSubSession(Api(), "bot", [("channel.chat.message", "1", {}), ("channel.cheer", "1", {})], on_event)
    await s.handle({"metadata": {"message_type": "session_welcome"}, "payload": {"session": {"id": "S1"}}})
    assert s.connected and created == [("bot", "channel.chat.message", "S1"), ("bot", "channel.cheer", "S1")]
    assert s.failed == ["channel.cheer"]
    await s.handle({"metadata": {"message_type": "notification"},
                    "payload": {"subscription": {"type": "channel.follow"}, "event": {"user_name": "x"}}})
    assert got == [("channel.follow", {"user_name": "x"})]
    await s.handle({"metadata": {"message_type": "session_reconnect"}, "payload": {"session": {"reconnect_url": "wss://new"}}})
    await s.handle({"metadata": {"message_type": "session_welcome"}, "payload": {"session": {"id": "S2"}}})
    assert len(created) == 2                                 # при переходе на новый адрес подписки не создаются заново


async def test_eventsub_retries_subscriptions_rejected_for_the_connection_limit(monkeypatch):
    import eventsub

    class FakeHelix:
        def __init__(self):
            self.calls = []

        async def subscribe(self, role, sub_type, version, condition, session_id):
            self.calls.append(sub_type)
            if len(self.calls) == 1:
                raise TwitchError(400, "number of websocket transports limit exceeded")

    monkeypatch.setattr(eventsub, "SUB_RETRY_S", (0.01, 0.01))
    helix = FakeHelix()
    session = EventSubSession(helix, "bot", [("channel.raid", "1", {})], lambda *a: None)
    session.session_id, session.connected = "s1", True
    later = await session._subscribe()
    assert later and session.failed == ["channel.raid"]
    await asyncio.wait_for(session._retry, 2)
    assert helix.calls == ["channel.raid", "channel.raid"] and session.failed == []     # повтор прошёл, отметка о сбое снята
