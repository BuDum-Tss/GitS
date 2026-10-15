"""Поток клиента модуля питомца против FakeCore (PET-3, PET-6, PET-12): тема presence → очередь → контроллер."""
import asyncio
import queue
import time

from gits_sdk import protocol as P
from gits_sdk.testing import FakeCore

import bridge
from manifest import MANIFEST, build_manifest
from pet_logic import PetController

SETTINGS = {"voice_muted": False, "pet_visible": True, "pet_pinned": False, "pet_x": None, "pet_y": None,
            "hotkey_input": "Ctrl+Shift+Space", "dnd": False, "quiet_until": 0.0}


class Recorder:
    def __init__(self):
        self.visible, self.frames = [], []

    def set_visible(self, v):
        self.visible.append(v)

    def draw(self, sticker, text, shown):
        self.frames.append((sticker, text, shown))


def test_manifest_matches_the_spec():
    m = build_manifest()
    assert P.validate_manifest(m) == []
    assert (m["id"], m["title"], m["capabilities"], m["subscribe"]) == ("pet", "Питомец", ["presence.view"],
                                                                         ["presence", "settings", "module"])
    assert m["shared_settings"] == ["voice_muted", "pet_visible", "pet_pinned", "pet_x", "pet_y", "hotkey_input",
                                    "dnd", "quiet_until"]
    assert not m.get("tools") and not m.get("emits") and m["state"] is False
    assert build_manifest() == MANIFEST and build_manifest() is not MANIFEST


def test_shared_settings_exist_in_the_core_settings():
    import settings
    for key in MANIFEST["shared_settings"]:
        assert key in settings.DEFAULTS, key


async def wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("не дождались")


async def test_client_thread_feeds_the_queue_and_the_controller():
    async with FakeCore(settings=SETTINGS) as core:
        q: queue.Queue = queue.Queue()
        thread = bridge.ClientThread(build_manifest(), q, url=core.url, token=core.token)
        thread.client.reconnect_delays = (0.8, 0.8)
        thread.start()
        try:
            await core.wait_connected(timeout=10)
            view = Recorder()
            c = PetController(view, q)

            def pumped(cond):
                c.pump()
                return cond()
            await wait(lambda: pumped(lambda: c.state.connected and c.state.settings.get("hotkey_input")))
            assert c.state.settings["pet_visible"] is True and c.state.enabled
            await core.topic("presence", {"speaking": True, "utterance_id": "u-1", "segment": "Идёт речь.", "emotion": "happy",
                                          "emotion_ts": time.time()})
            await wait(lambda: pumped(lambda: view.frames and view.frames[-1] == ("happy", "Идёт речь.", True)))
            await core.update_settings({"pet_pinned": True, "voice_muted": True})
            await wait(lambda: pumped(lambda: c.state.pinned and c.state.muted))
            await core.set_enabled(False)
            await wait(lambda: pumped(lambda: not c.state.enabled))
            assert view.visible[-1] is False                                 # выключен: окно скрыто, клиент подключён
            assert thread.client.connected
            await core.drop()
            await wait(lambda: pumped(lambda: not c.state.connected and view.frames[-1][0] == "resting"))
            await wait(lambda: pumped(lambda: c.state.connected))             # переподключился сам
            await core.shutdown("тест")
            await wait(lambda: not thread.is_alive())
            assert thread.exit_code == 0
        finally:
            thread.request_stop()
            thread.join(5)


async def test_client_thread_ends_when_the_core_replaces_the_module():
    async with FakeCore(settings=SETTINGS) as core:
        q: queue.Queue = queue.Queue()
        thread = bridge.ClientThread(build_manifest(), q, url=core.url, token=core.token)
        thread.start()
        try:
            await core.wait_connected(timeout=10)
            await core.kick()
            await wait(lambda: not thread.is_alive())
            assert thread.exit_code == 0
        finally:
            thread.request_stop()
            thread.join(5)
