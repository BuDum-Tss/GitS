import asyncio
from base64 import b64encode
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gits_sdk import ToolError, WorkspaceClient
from gits_sdk.protocol import validate_manifest

from workspaces.ktalk.browser import KTalkBrowser
from workspaces.ktalk.main import KTalk, MANIFEST, meeting_url

URL = "https://itmo.ktalk.ru/test-room"


@pytest.mark.parametrize("url", [
    "http://itmo.ktalk.ru/test-room",
    "https://itmo.ktalk.ru.example.com/test-room",
    "https://itmo.ktalk.ru@other.example/test-room",
    "https://itmo.ktalk.ru/a/b",
])
def test_rejects_non_room_urls(url):
    with pytest.raises(ToolError):
        meeting_url(url)


def test_manifest_and_query():
    assert not validate_manifest(MANIFEST)
    assert meeting_url(f" {URL}?pin=123 ") == f"{URL}?pin=123"


def test_binding_accepts_only_expected_frame_and_pcm():
    browser = KTalkBrowser()
    browser.origin = "https://itmo.ktalk.ru"
    frame = SimpleNamespace(url=URL)
    browser.page = SimpleNamespace(main_frame=frame)
    data = b"\x01" * 2048
    encoded = b64encode(data).decode()
    # Use a different URL so an unrelated frame cannot impersonate the main one.
    browser.receive({"frame": SimpleNamespace(url="https://other.example")}, "1", encoded)
    browser.receive({"frame": frame}, "x", encoded)
    browser.receive({"frame": frame}, "1", "broken")
    assert browser.incoming.empty()
    browser.receive({"frame": frame}, "1", encoded)
    browser.receive({"frame": frame}, "1", None)
    assert browser.incoming.get_nowait() == ("1", data)
    assert browser.incoming.get_nowait() == ("1", None)


async def test_separate_pcm_and_track_lifecycle():
    ws = KTalk(WorkspaceClient(MANIFEST))
    voice = SimpleNamespace(ready=True, speaker_join=AsyncMock(), send_audio=AsyncMock(), speaker_leave=AsyncMock())
    browser = SimpleNamespace(incoming=asyncio.Queue())
    await ws.register_speakers(voice)
    for event in [("1", b"a" * 2048), ("2", b"b" * 2048), ("1", b"a" * 2048), ("1", None)]:
        browser.incoming.put_nowait(event)
    await ws.forward_audio(browser, voice)
    assert [item.args for item in voice.speaker_join.await_args_list] == [("1", "Участник 1"), ("2", "Участник 2")]
    for item in voice.send_audio.await_args_list:
        speaker, pcm = item.args
        assert pcm == (b"a" if speaker == "1" else b"b") * 640
    assert len(voice.send_audio.await_args_list) == 9
    voice.speaker_leave.assert_awaited_once_with("1")
    assert set(ws.buffers) == {"2"}


def test_name_binding_checks_frame_and_cleans_text():
    browser = KTalkBrowser()
    browser.origin = "https://itmo.ktalk.ru"
    frame = SimpleNamespace(url=URL)
    browser.page = SimpleNamespace(main_frame=frame)
    browser.receive_name({"frame": SimpleNamespace(url="https://other.example")}, "1", "Чужой")
    browser.receive_name({"frame": frame}, "x", "Чужой")
    browser.receive_name({"frame": frame}, "1", 5)
    browser.receive_name({"frame": frame}, "1", " \x07 ")
    assert browser.names == {}
    browser.receive_name({"frame": frame}, "1", " Анна\x07 Потапова " + "я" * 100)
    assert browser.names["1"].startswith("Анна Потапова") and len(browser.names["1"]) <= 64


async def test_late_name_renames_speaker_and_known_name_is_used_on_join():
    ws = KTalk(WorkspaceClient(MANIFEST))
    voice = SimpleNamespace(ready=True, speaker_join=AsyncMock(), send_audio=AsyncMock(), speaker_leave=AsyncMock())
    browser = SimpleNamespace(incoming=asyncio.Queue(), names={"2": "Борис"})
    await ws.register_speakers(voice)
    browser.incoming.put_nowait(("1", b"a" * 2048))
    browser.incoming.put_nowait(("2", b"b" * 2048))
    await ws.forward_audio(browser, voice)
    assert [item.args for item in voice.speaker_join.await_args_list] == [("1", "Участник 1"), ("2", "Борис")]
    browser.names["1"] = "Анна"
    await ws.forward_audio(browser, voice)
    assert voice.speaker_join.await_args_list[-1].args == ("1", "Анна")
    await ws.forward_audio(browser, voice)
    assert len(voice.speaker_join.await_args_list) == 3
