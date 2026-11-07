"""Модуль OBS: инструменты управления трансляцией, события и состояние поверх клиента obs-websocket."""
import asyncio
import logging
import os

from gits_sdk import ToolError

from obs_client import ObsClient, ObsError

log = logging.getLogger("obs")

DEFAULT_URL = "ws://127.0.0.1:4455"
STATE_EVERY_S = 30.0
STATE_POLL_S = 2.0
CAPTION_MAX = 200


def _scene_names(data: dict) -> list[str]:
    return [str(s.get("sceneName")) for s in reversed(data.get("scenes") or []) if s.get("sceneName")]      # OBS отдаёт снизу вверх


def pick_scene(wanted: str, names: list[str]) -> str:
    """Сцена по названию без учёта регистра и пробелов по краям; неизвестное имя: ToolError со списком сцен."""
    key = (wanted or "").strip().casefold()
    for name in names:
        if name.casefold() == key:
            return name
    raise ToolError(f"сцены «{wanted}» нет. Есть: {', '.join(names) or 'пусто'}", code="bad_request")


class ObsModule:
    def __init__(self, client, url: str | None = None, password: str | None = None):
        self.client = client
        self.obs = ObsClient(url or os.getenv("OBS_WEBSOCKET_URL") or DEFAULT_URL,
                             password if password is not None else os.getenv("OBS_WEBSOCKET_PASSWORD", ""), self._on_event)
        self.scene = ""
        self.streaming: bool | None = None
        self.recording: bool | None = None
        self.tasks: list[asyncio.Task] = []
        self._register()

    def start(self) -> list[asyncio.Task]:
        loop = asyncio.get_running_loop()
        for coro in (self.obs.run(), self._state_loop()):
            self.tasks.append(loop.create_task(coro))
        return self.tasks

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.obs.close()

    # ── события OBS ──────────────────────────────────────────────────────────
    async def _on_event(self, kind: str, data: dict) -> None:
        if kind == "CurrentProgramSceneChanged":
            self.scene = str(data.get("sceneName") or "")
            await self.client.event("obs.scene", f"Сцена OBS: {self.scene}.", announce=False, memorable=False)
        elif kind == "StreamStateChanged" and data.get("outputState") in ("OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_STOPPED"):
            self.streaming = data["outputState"].endswith("STARTED")
            await self.client.event("obs.stream_state", "Трансляция в OBS началась." if self.streaming else "Трансляция в OBS закончилась.",
                                    announce=False)
        elif kind == "RecordStateChanged" and data.get("outputState") in ("OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_STOPPED"):
            self.recording = data["outputState"].endswith("STARTED")
            text = "Запись в OBS началась." if self.recording else f"Запись в OBS остановлена: {data.get('outputPath') or 'файл неизвестен'}."
            await self.client.event("obs.record_state", text, announce=False, memorable=False)

    # ── инструменты ──────────────────────────────────────────────────────────
    async def _req(self, request_type: str, data: dict | None = None) -> dict:
        try:
            return await self.obs.request(request_type, data)
        except ObsError as e:
            raise ToolError(str(e), code="unavailable") from e

    async def refresh(self) -> None:
        """Подтянуть текущее состояние из OBS (сцена, трансляция, запись)."""
        scene = await self._req("GetCurrentProgramScene")
        self.scene = str(scene.get("sceneName") or scene.get("currentProgramSceneName") or "")
        self.streaming = bool((await self._req("GetStreamStatus")).get("outputActive"))
        self.recording = bool((await self._req("GetRecordStatus")).get("outputActive"))

    def _register(self) -> None:
        c = self.client

        @c.tool("status")
        async def status(args):
            await self.refresh()
            return {"connected": True, "scene": self.scene, "streaming": self.streaming, "recording": self.recording}

        @c.tool("scenes")
        async def scenes(args):
            data = await self._req("GetSceneList")
            return {"scenes": _scene_names(data), "current": data.get("currentProgramSceneName", "")}

        @c.tool("switch_scene")
        async def switch_scene(args):
            name = pick_scene(str(args.get("scene") or ""), _scene_names(await self._req("GetSceneList")))
            await self._req("SetCurrentProgramScene", {"sceneName": name})
            self.scene = name
            return {"scene": name}

        @c.tool("start_stream")
        async def start_stream(args):
            await self.refresh()
            if self.streaming:
                return {"streaming": True, "note": "трансляция уже идёт"}
            await self._req("StartStream")
            return {"streaming": True}

        @c.tool("stop_stream")
        async def stop_stream(args):
            await self.refresh()
            if not self.streaming:
                return {"streaming": False, "note": "трансляция не идёт"}
            await self._req("StopStream")
            return {"streaming": False}

        @c.tool("start_record")
        async def start_record(args):
            await self.refresh()
            if self.recording:
                return {"recording": True, "note": "запись уже идёт"}
            await self._req("StartRecord")
            return {"recording": True}

        @c.tool("stop_record")
        async def stop_record(args):
            await self.refresh()
            if not self.recording:
                return {"recording": False, "note": "запись не идёт"}
            data = await self._req("StopRecord")
            return {"recording": False, "path": data.get("outputPath", "")}

        @c.tool("caption")
        async def caption(args):
            text = " ".join(str(args.get("text") or "").split())[:CAPTION_MAX]
            if not text:
                raise ToolError("пустой субтитр", code="bad_request")
            await self._req("SendStreamCaption", {"captionText": text})
            return {"sent": True}

    # ── состояние для ядра ───────────────────────────────────────────────────
    def snapshot(self) -> tuple[str, dict]:
        if not self.obs.connected:
            hint = f" ({self.obs.last_error})" if self.obs.last_error else ""
            return f"OBS: не на связи{hint}", {"connected": False}
        live = {True: "в эфире", False: "не в эфире", None: "эфир неизвестен"}[self.streaming]
        rec = "пишет" if self.recording else "не пишет"
        return f"OBS: сцена «{self.scene or '?'}», {live}, {rec}", {
            "connected": True, "scene": self.scene, "streaming": self.streaming, "recording": self.recording}

    async def _state_loop(self) -> None:
        last_key, sent_at, loop = None, 0.0, asyncio.get_running_loop()
        while True:
            try:
                if self.obs.connected and self.streaming is None:
                    try:
                        await self.refresh()
                    except ToolError:
                        pass
                summary, data = self.snapshot()
                key = (summary,)
                if key != last_key or loop.time() - sent_at >= STATE_EVERY_S:
                    await self.client.state(summary, data)
                    last_key, sent_at = key, loop.time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("состояние не отправлено: %s", e)
            await asyncio.sleep(STATE_POLL_S)
