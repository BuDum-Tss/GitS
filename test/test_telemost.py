import asyncio
from pathlib import Path

import numpy as np
import pytest
import yaml
from gits_sdk import ToolError, WorkspaceClient
from gits_sdk.protocol import validate_manifest, validate_panel_blocks
from gits_sdk.testing import FakeCore

from workspaces.telemost.main import MANIFEST, SPEAKER, Telemost, meeting_id
from workspaces.telemost.browser import FRAME_BYTES
from agent.services.voice_engine.diarization import SpeakerLabels

LINK = "https://telemost.yandex.ru/j/1234567890"


@pytest.mark.parametrize("link", [
    "https://telemost.yandex.ru.example.com/j/123",
    "https://telemost.yandex.ru@other.example/j/123",
    "https://telemost.yandex.ru/j/123/456",
    "sip:123@sip.t.ya.ru",
    None,
])
def test_rejects_invalid_links(link):
    with pytest.raises(ToolError, match="Нужна ссылка"):
        meeting_id(link)


def test_manifest_and_link():
    assert not validate_manifest(MANIFEST)
    assert meeting_id(LINK + "/?ignored=yes") == "1234567890"
    for name in ("agent/peers.yaml", "agent/peers.container.yaml"):
        entries = yaml.safe_load(Path(name).read_text(encoding="utf-8"))["workspaces"]
        entry = next(entry for entry in entries if entry["id"] == MANIFEST["id"])
        assert entry["cwd"].endswith("/workspaces/telemost")


class FakeBrowser:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.ended = False
        self.closed = False

    async def open(self, number):
        assert number == "1234567890"
        self.incoming.put_nowait(bytes(FRAME_BYTES))

    async def poll(self):
        return "ended" if self.ended else "connected"

    async def close(self):
        self.closed = True

    async def play(self, *_):
        return 0

    def stop_playback(self):
        pass


class FakeVoice:
    ready = True

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.frames = asyncio.Queue()
        self.closed = False

    async def start(self):
        pass

    async def wait_ready(self, _timeout):
        return True

    async def speaker_join(self, speaker, _name):
        assert speaker == SPEAKER

    async def send_audio(self, speaker, pcm):
        self.frames.put_nowait((speaker, pcm))

    async def stop(self):
        self.closed = True


async def test_call_from_dialog_and_dashboard():
    calls, voices = [], []

    def browser_factory():
        browser = FakeBrowser()
        calls.append(browser)
        return browser

    def voice_factory(**kwargs):
        voice = FakeVoice(**kwargs)
        voices.append(voice)
        return voice

    async with FakeCore() as core:
        client = WorkspaceClient(MANIFEST, url=core.url, token=core.token)
        module = Telemost(client, browser_factory, voice_factory)
        client_task = asyncio.create_task(client.run())
        panel_task = asyncio.create_task(module.publish())
        try:
            await asyncio.wait_for(client.wait_connected(), 3)
            blocks = await core.wait_message("panel.set")
            assert not validate_panel_blocks(blocks.body["blocks"], {"join", "leave"})
            await core.call_tool("telemost.voice_join", {"link": LINK})
            assert await asyncio.wait_for(voices[0].frames.get(), 3) == (SPEAKER, bytes(FRAME_BYTES))
            assert module.status == "В звонке через браузер"
            assert not voices[0].kwargs["speakers_labelled"]
            assert "1234567890" not in voices[0].kwargs["session"]
            with pytest.raises(ToolError, match="Сначала отключитесь"):
                await core.call_tool("telemost.voice_join", {"link": LINK})
            await core.call_tool("telemost.voice_leave")
            assert calls[0].closed and voices[0].closed

            assert (await core.panel_action("join", {"value": LINK}))["ok"]
            assert await asyncio.wait_for(voices[1].frames.get(), 3) == (SPEAKER, bytes(FRAME_BYTES))
            calls[1].ended = True
            await asyncio.wait_for(module.task, 3)
            assert calls[1].closed and voices[1].closed
            assert module.status == "Нет активного звонка"
        finally:
            await module.disconnect()
            panel_task.cancel()
            client_task.cancel()
            await asyncio.gather(panel_task, client_task, return_exceptions=True)


async def test_labels_are_stable_and_uncertain_phrases_remain_unlabelled():
    vectors = iter(([1, 0], [0, 1], [0.99, 0.01], [0.71, 0.71], [0, 0]))
    labels = SpeakerLabels(lambda _: np.array(next(vectors)))
    assert await labels.identify(b"", 400) is None
    assert await labels.identify(b"", 2000) == 1
    assert await labels.identify(b"", 2000) == 2
    assert await labels.identify(b"", 2000) == 1
    assert await labels.identify(b"", 2000) is None
    assert await labels.identify(b"", 2000) is None
    assert await SpeakerLabels(lambda _: np.array([0, 1])).identify(b"", 2000) == 1

async def test_browser_enters_as_guest_and_waits_for_admission(monkeypatch):
    from unittest.mock import AsyncMock

    from workspaces.telemost import browser as ui

    class Control:
        def __init__(self, visible=False):
            self.first = self
            self.is_visible = AsyncMock(return_value=visible)
            self.is_enabled = AsyncMock(return_value=True)
            self.is_editable = AsyncMock(return_value=True)
            self.input_value = AsyncMock(return_value="Гость")
            self.fill = AsyncMock()
            self.click = AsyncMock()

        def or_(self, _):
            return self

    class Page:
        def __init__(self):
            self.controls = {pattern: Control(pattern in (ui.NAME, ui.JOIN)) for pattern in
                             (ui.JOIN, ui.LEAVE, ui.WEB, ui.NAME, ui.MIC_OFF, ui.CAMERA_ON, ui.WAITING, ui.AUTH)}

        def is_closed(self):
            return False

        def get_by_text(self, pattern):
            return self.controls[pattern]

        def get_by_role(self, _role, *, name):
            return self.controls[name]

        def get_by_placeholder(self, pattern):
            return self.controls[pattern]

    call = ui.BrowserCall()
    call.page = Page()
    controls = call.page.controls
    capture = AsyncMock()
    monkeypatch.setattr(call, "start_capture", capture)
    assert ui.JOIN.fullmatch("Подключиться к встрече")
    assert await call.poll() == "connecting"
    controls[ui.NAME].fill.assert_awaited_once_with("ГЛаДОС", timeout=1500)
    controls[ui.JOIN].click.assert_awaited_once()
    controls[ui.WAITING].is_visible.return_value = True
    controls[ui.LEAVE].is_visible.return_value = True
    assert await call.poll() == "waiting"
    capture.assert_not_awaited()
    controls[ui.WAITING].is_visible.return_value = False
    assert await call.poll() == "connected"
    assert await call.poll() == "connected"
    capture.assert_awaited_once()

    gated = ui.BrowserCall()
    gated.page = Page()
    gated.page.controls[ui.JOIN].is_visible.return_value = False
    gated.page.controls[ui.AUTH].is_visible.return_value = True
    assert await gated.poll() == "connecting"
    gated.auth_since -= 3
    assert await gated.poll() == "auth_required"

async def test_browser_reports_played_audio_when_pacat_drain_times_out(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from workspaces.telemost import browser as ui

    process = Mock(returncode=None)
    process.stdin.drain = AsyncMock()
    process.wait = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr(ui.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    stop = AsyncMock()
    monkeypatch.setattr(ui, "stop_process", stop)

    class Frames:
        stop_reason = None

        async def __aiter__(self):
            for _ in range(4):
                yield bytes(FRAME_BYTES)

    call = ui.BrowserCall()
    call.audio_env = {}
    assert 0 < await call.play(Frames(), "reply") <= 80
    assert process.stdin.write.call_count == 4
    stop.assert_awaited_once_with(process)
    assert call.player is None
