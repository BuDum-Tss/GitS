"""Модуль OBS против поддельного obs-websocket и FakeCore: рукопожатие с паролем, инструменты, события, состояние."""
import asyncio
import json

import pytest
from aiohttp import web
from gits_sdk import ModuleClient, ToolError
from gits_sdk.testing import FakeCore

import obs_client
from manifest import build_manifest
from obs_module import ObsModule, pick_scene

PASSWORD = "секрет-пароль"


class FakeObs:
    """Минимальный obs-websocket v5: Hello с вызовом, Identify, запросы, события."""

    def __init__(self, password=PASSWORD):
        self.password, self.salt, self.challenge = password, "соль", "вызов"
        self.requests: list[tuple[str, dict]] = []
        self.scene, self.streaming, self.recording = "Кабинет", False, False
        self.sockets: list[web.WebSocketResponse] = []
        self.runner = None
        self.url = ""

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"

    async def stop(self):
        await self.runner.cleanup()

    async def event(self, kind, data):
        for ws in self.sockets:
            await ws.send_json({"op": 5, "d": {"eventType": kind, "eventData": data}})

    async def _ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"challenge": self.challenge, "salt": self.salt}
        await ws.send_json({"op": 0, "d": hello})
        ident = json.loads((await ws.receive()).data)["d"]
        if self.password and ident.get("authentication") != obs_client.auth_string(self.password, self.salt, self.challenge):
            await ws.close(code=4009)
            return ws
        await ws.send_json({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        self.sockets.append(ws)
        async for msg in ws:
            d = json.loads(msg.data)["d"]
            kind, data = d["requestType"], d.get("requestData") or {}
            self.requests.append((kind, data))
            status, resp = {"result": True, "code": 100}, {}
            if kind == "GetSceneList":
                resp = {"currentProgramSceneName": self.scene, "scenes": [{"sceneName": "Игра"}, {"sceneName": "AFK"}, {"sceneName": "Кабинет"}]}
            elif kind == "GetCurrentProgramScene":
                resp = {"sceneName": self.scene}
            elif kind == "SetCurrentProgramScene":
                self.scene = data["sceneName"]
            elif kind == "GetStreamStatus":
                resp = {"outputActive": self.streaming}
            elif kind == "StartStream":
                self.streaming = True
            elif kind == "StopStream":
                self.streaming = False
            elif kind == "GetRecordStatus":
                resp = {"outputActive": self.recording}
            elif kind == "StartRecord":
                self.recording = True
            elif kind == "StopRecord":
                self.recording, resp = False, {"outputPath": "C:/rec/a.mp4"}
            elif kind == "SendStreamCaption":
                pass
            else:
                status = {"result": False, "code": 204, "comment": "неизвестный запрос"}
            await ws.send_json({"op": 7, "d": {"requestType": kind, "requestId": d["requestId"], "requestStatus": status, "responseData": resp}})
        return ws


class Rig:
    def __init__(self, password=PASSWORD, give_password=True):
        self.fake = FakeObs(password)
        self.give_password = give_password
        self.core = FakeCore(settings={})

    async def __aenter__(self):
        await self.fake.start()
        await self.core.start()
        self.client = ModuleClient(build_manifest(), url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.mod = ObsModule(self.client, self.fake.url, PASSWORD if self.give_password else "")
        obs_client.RECONNECT_S = 0.1
        self.tasks = self.mod.start()
        self.run_task = asyncio.create_task(self.client.run())
        await self.core.wait_connected()
        for _ in range(100):
            if self.mod.obs.connected:
                break
            await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        await self.mod.stop()
        await self.client.stop()
        await asyncio.wait_for(self.run_task, 10)
        await self.core.stop()
        await self.fake.stop()

    async def call(self, tool, args=None):
        return await self.core.call_tool(tool, args or {})


def test_auth_string_follows_the_documented_scheme():
    """base64(sha256(base64(sha256(пароль+соль))+вызов)): схема из описания протокола obs-websocket v5."""
    import base64
    import hashlib
    secret = base64.b64encode(hashlib.sha256(b"pwdSALT").digest()).decode()
    want = base64.b64encode(hashlib.sha256((secret + "CHALLENGE").encode()).digest()).decode()
    assert obs_client.auth_string("pwd", "SALT", "CHALLENGE") == want


def test_manifest_has_the_tools_and_valid_risks():
    from gits_sdk.protocol import validate_manifest
    m = build_manifest()
    assert validate_manifest(m) == []
    risks = {t["name"]: t["risk"] for t in m["tools"]}
    assert risks["start_stream"] == "public" and risks["stop_stream"] == "ask" and risks["switch_scene"] == "auto"


def test_pick_scene_is_case_insensitive_and_lists_options_on_miss():
    assert pick_scene("afk", ["Кабинет", "AFK", "Игра"]) == "AFK"
    with pytest.raises(ToolError) as e:
        pick_scene("нет такой", ["Кабинет", "AFK"])
    assert "Кабинет, AFK" in str(e.value)


async def test_tools_switch_scene_stream_and_record():
    async with Rig() as rig:
        assert (await rig.call("scenes"))["scenes"] == ["Кабинет", "AFK", "Игра"]
        assert (await rig.call("switch_scene", {"scene": "игра"}))["scene"] == "Игра" and rig.fake.scene == "Игра"
        assert (await rig.call("start_stream"))["streaming"] is True and rig.fake.streaming
        again = await rig.call("start_stream")
        assert again["note"] and [r for r in rig.fake.requests if r[0] == "StartStream"] == [("StartStream", {})]
        assert (await rig.call("stop_stream"))["streaming"] is False
        await rig.call("start_record")
        assert (await rig.call("stop_record"))["path"] == "C:/rec/a.mp4"
        assert (await rig.call("caption", {"text": "  привет   мир "}))["sent"]
        assert ("SendStreamCaption", {"captionText": "привет мир"}) in rig.fake.requests
        st = await rig.call("status")
        assert st["scene"] == "Игра" and st["streaming"] is False


async def test_unknown_scene_and_empty_caption_are_rejected():
    async with Rig() as rig:
        with pytest.raises(Exception) as e:
            await rig.call("switch_scene", {"scene": "нет"})
        assert "Кабинет" in str(e.value)
        with pytest.raises(Exception, match="пустой"):
            await rig.call("caption", {"text": "  "})


async def test_events_become_core_events_and_state_shows_scene():
    async with Rig() as rig:
        await rig.fake.event("StreamStateChanged", {"outputState": "OBS_WEBSOCKET_OUTPUT_STARTED"})
        await rig.fake.event("CurrentProgramSceneChanged", {"sceneName": "AFK"})
        for _ in range(100):
            kinds = [m.body.get("kind") for m in rig.core.messages_of("event")]
            if "obs.stream_state" in kinds and "obs.scene" in kinds:
                break
            await asyncio.sleep(0.05)
        assert "obs.stream_state" in kinds and "obs.scene" in kinds
        assert rig.mod.streaming is True and rig.mod.scene == "AFK"
        summary, data = rig.mod.snapshot()
        assert "AFK" in summary and "в эфире" in summary and data["connected"] is True


async def test_wrong_password_keeps_module_disconnected_with_a_reason():
    rig = Rig(give_password=False)
    await rig.fake.start()
    await rig.core.start()
    client = ModuleClient(build_manifest(), url=rig.core.url, token=rig.core.token)
    mod = ObsModule(client, rig.fake.url, "")
    obs_client.RECONNECT_S = 0.1
    task = asyncio.create_task(mod.obs.run())
    await asyncio.sleep(0.5)
    summary, data = mod.snapshot()
    assert data["connected"] is False and "OBS_WEBSOCKET_PASSWORD" in summary
    assert PASSWORD not in summary
    with pytest.raises(ToolError):
        await mod._req("GetSceneList")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await mod.obs.close()
    await rig.core.stop()
    await rig.fake.stop()
