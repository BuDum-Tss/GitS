"""Стенд тестов модуля Telegram: настоящий ModuleClient против FakeCore, сеть Telegram и Telethon подменены."""
import asyncio
import contextlib
import json
from pathlib import Path

from gits_sdk import ModuleClient
from gits_sdk.testing import FakeCore

import events_log
from telegram_module import TelegramModule
from manifest import build_manifest

SHARED = {"telegram": False, "telegram_voice": False, "telegram_confirm": False, "quiet_until": 0.0, "dnd": False}
BOT_ME = {"id": 42, "username": "glados_bot"}


class FakeTelethon:
    """Клиент Telethon: запоминает, что в него отправили."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.files: list[dict] = []

    def is_connected(self):
        return True

    async def get_input_entity(self, chat_id):
        return ("entity", chat_id)

    async def send_message(self, ent, text):
        self.sent.append((ent, text))

    async def send_file(self, ent, file, voice_note=False, caption=""):
        self.files.append({"ent": ent, "data": file.read(), "name": file.name, "voice_note": voice_note, "caption": caption})


class NetStubs:
    """Подмена сети Telegram: Bot API (`_call`, `_upload` канала) и Telethon (`_connect` аккаунта)."""

    def __init__(self):
        self.api: list[tuple[str, dict]] = []             # вызовы Bot API: (метод, параметры)
        self.uploads: list[tuple[str, dict, dict]] = []
        self.updates: list[list] = []                     # очередь ответов getUpdates
        self.tl = FakeTelethon()

    def install(self, mod: TelegramModule) -> None:
        stubs = self

        async def call(method, _timeout=15, **params):
            stubs.api.append((method, params))
            if method == "getMe":
                return BOT_ME
            if method == "getChat":
                return {"title": "Канал"}
            if method == "getChatMember":
                return {"status": "administrator", "can_post_messages": True}
            if method == "getUpdates":
                if stubs.updates:
                    return stubs.updates.pop(0)
                await asyncio.sleep(3600)
            return {"message_id": 1}

        async def upload(method, data, files):
            stubs.uploads.append((method, data, files))
            return {"message_id": 2}

        async def connect():
            return stubs.tl

        mod.channel._call = call
        mod.channel._upload = upload
        mod.user._connect = connect


class Rig:
    def __init__(self, tmp_path, monkeypatch, *, settings=None, module_settings=None, terms=("Анна",),
                 token="123:SECRET-TOKEN", channel="@chan", telethon=False, privacy_ready=True):
        self.tmp = Path(tmp_path)
        self.mem = self.tmp / "memory"
        self.mem.mkdir(parents=True, exist_ok=True)
        self.terms = list(terms)
        self.asks: list[dict] = []
        self.ask_answer = True
        self.tts_calls: list[dict] = []
        self.net = NetStubs()
        if token:
            monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
            monkeypatch.setenv("TELEGRAM_CHANNEL", channel)
        if telethon:
            monkeypatch.setenv("TELEGRAM_API_ID", "1")
            monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
            (self.mem / "tg_user.session").write_bytes(b"")
        self.core = FakeCore(settings={**SHARED, **(settings or {})}, module_settings=module_settings)
        self.tool_calls: list[dict] = []
        self._privacy_ready = privacy_ready
        self._task: asyncio.Task | None = None

    def _build(self):
        """Клиент и модуль создаются после старта FakeCore: адрес ядра известен только тогда."""
        self.client = ModuleClient(build_manifest(), url=self.core.url, token=self.core.token)
        self.client.reconnect_delays = (0.05, 0.05, 0.1)
        self.mod = TelegramModule(self.client, str(self.mem))
        # персона от ядра (core.persona, ТЗ 05 CHR-4): в тестах заглушка с теми же разделами и именами
        self.mod.channel.set_persona({"telegram_channel": "Ты — тестовый персонаж канала.",
                                      "telegram_formats": "- тестовый формат"})
        self.mod.user.set_aliases(["ГЛаДОС", "glados"])
        self._wire()

    def _wire(self):
        core, rig = self.core, self

        @core.on_request("core.privacy_terms")
        def privacy(body):
            return {"terms": rig.terms}

        @core.on_request("core.ask")
        def ask(body):
            rig.asks.append(body)
            return {"answer": rig.ask_answer, "by": "user"}

        @core.on_request("core.tts")
        def tts(body):
            rig.tts_calls.append(body)
            path = rig.tmp / "tts_test.ogg"
            path.write_bytes(b"OggS-voice")
            return {"path": str(path), "duration_ms": 1000}

        @core.on_request("core.tool.call")
        def tool_call(body):
            rig.tool_calls.append(body)
            return {"text": "ok"}

        self.net.install(self.mod)

    async def __aenter__(self):
        await self.core.start()
        self._build()
        self._task = asyncio.create_task(self.client.run())
        await self.core.wait_connected()
        await self.client.wait_connected(5)
        if self._privacy_ready:
            assert await self.mod.privacy.refresh()
        return self

    async def __aexit__(self, *exc):
        await self.mod.stop()
        await self.client.stop()
        if self._task is not None and not self._task.done():
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._task, 5)
        await self.core.stop()

    # ── удобства ──

    api = property(lambda self: self.net.api)
    uploads = property(lambda self: self.net.uploads)
    updates = property(lambda self: self.net.updates)
    tl = property(lambda self: self.net.tl)

    def api_calls(self, method):
        return [p for m, p in self.net.api if m == method]

    def journal(self, kind="telegram"):
        events_log.flush()
        return events_log.tail(100, {kind})

    def read_state(self):
        return json.loads((self.mem / "telegram.json").read_text(encoding="utf-8"))

    def stub_model(self, monkeypatch, text, post=True):
        """Локальная модель отвечает заданным текстом поста."""
        import fast_brain

        async def fake_ask(system, user, **kw):
            return json.dumps({"post": post, "text": text}, ensure_ascii=False)
        monkeypatch.setattr(fast_brain, "_ask", fake_ask)
