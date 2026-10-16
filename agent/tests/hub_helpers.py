"""Общее для тестов шины модулей: ядро в миниатюре на локальном aiohttp и «сырой» модуль для проверки протокола."""
import asyncio
import contextlib
import json
from pathlib import Path

import aiohttp
from aiohttp import web

import speech_out
from conversation_io import current_channel
from adapters.module_events import AttentionEventSink
from attention import Attention
from core_requests import CoreRequests
from environments.registry import Registry
from gits_sdk import protocol as P
from gits_sdk.protocol import Envelope
from module_hub import ModuleHub
from module_registry import ModuleRegistry
from router import Router


class FakeSpeech:
    """Вывод речи в памяти: что произнесено, что остановлено; говорит ли она, задаётся тестом."""

    def __init__(self):
        self.spoken: list[tuple] = []
        self.stopped: list = []
        self.speaking = False

    async def speak(self, utterance_id, text, emotion, priority):
        self.spoken.append((utterance_id, text, emotion, priority))

    async def stop(self, utterance_id=None):
        self.stopped.append(utterance_id)
        self.speaking = False

    def is_speaking(self):
        return self.speaking

    def current_segment(self):
        return ""


class FakeCompanion:
    """То, что нужно менеджеру внимания от компаньона."""

    def __init__(self):
        self.said: list[tuple] = []
        self.about: list[tuple] = []
        self.speech = FakeSpeech()
        self.presence = speech_out.PresenceTracker()
        self._last_user_ts = 0.0
        self.graph = None
        self.last_utterance_id = None
        self.repeats = False
        self.about_channels: list = []                  # канал обращения, в котором вызван _speak_about

    @property
    def speaking(self):
        return self.speech.speaking

    @speaking.setter
    def speaking(self, value):
        self.speech.speaking = value

    async def say(self, text, emotion=None, priority=None, *, direct=False, source=None):
        self.said.append((text, emotion))
        self.last_utterance_id = "u-" + str(len(self.said))

    async def _speak_about(self, fact, user_text="", must=False, question=False, direct=False):
        self.about.append((fact, must, question))
        self.about_channels.append(current_channel.get())

    def _repeats_recent(self, text):
        return self.repeats

    def is_speaking(self):
        return self.speech.is_speaking()


class FakeIO:
    def __init__(self):
        self.inputs: list[tuple] = []

    async def from_module(self, text, reply_channel, sender=None):
        self.inputs.append((text, reply_channel, sender))


class Core:
    """Шина, реестры, маршрутизатор и заглушка внимания на настоящем HTTP-сервере 127.0.0.1."""

    def __init__(self, tmp_path: Path, settings_state: dict, spoof_remote: str | None = None):
        self.settings_state = settings_state          # то, что «лежит в settings.json» (подменяется в тестах)
        self.envs = Registry()
        self.modules = ModuleRegistry(self.envs, settings_source=lambda: self.settings_state)
        self.companion = FakeCompanion()
        self.io = FakeIO()
        self.attention = Attention(self.companion, self.io, idle_source=lambda: 0.0)       # не зависеть от простоя настоящего ввода
        self.attention.hub, self.attention.modules = None, self.modules
        self.requests = CoreRequests(attention=self.attention)
        self.said: list[str] = []
        self.hub = ModuleHub(self.modules, sink=AttentionEventSink(self.attention), requests=self.requests,
                             settings_source=lambda: self.settings_state, token_file=tmp_path / "module_token",
                             hello_timeout=0.5)
        self.router = Router(self.envs, self._say, lambda: "")
        self.requests.router = self.router
        self.modules.supervisor = None
        self._spoof = spoof_remote
        self._runner: web.AppRunner | None = None
        self.port = 0

    async def _say(self, text, *a, **kw):
        self.said.append(text)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/module/ws"

    @property
    def token(self) -> str:
        return self.hub.token

    async def start(self):
        middlewares = []
        if self._spoof:
            @web.middleware
            async def spoof(request, handler):
                return await handler(request.clone(remote=self._spoof))
            middlewares.append(spoof)
        app = web.Application(middlewares=middlewares)
        self.hub.attach(app)
        self._runner = web.AppRunner(app, shutdown_timeout=0.5)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = self._runner.addresses[0][1]
        await self.hub.start()

    async def stop(self):
        await self.attention.stop()
        await self.hub.stop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def wait_for(self, predicate, timeout=5.0):
        async def waiter():
            while True:
                value = predicate()
                if value:
                    return value
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(waiter(), timeout)

    async def wait_connected(self, module_id: str, timeout=5.0):
        await self.wait_for(lambda: self.hub.connection(module_id) is not None, timeout)


@contextlib.asynccontextmanager
async def running_core(tmp_path, settings_state=None, spoof_remote=None):
    core = Core(tmp_path, {"modules": {}} if settings_state is None else settings_state, spoof_remote)
    await core.start()
    try:
        yield core
    finally:
        await core.stop()


@contextlib.asynccontextmanager
async def running_client(client, core):
    """Запустить ModuleClient против ядра и дождаться рукопожатия."""
    task = asyncio.create_task(client.run())
    try:
        await core.wait_connected(client.module_id)
        await client.wait_connected(5)
        yield task
    finally:
        await client.stop()
        if not task.done():
            try:
                await asyncio.wait_for(task, 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()


class RawModule:
    """Модуль без SDK: шлёт и читает кадры как есть (проверка протокола на стороне сервера)."""

    def __init__(self, core: Core, manifest: dict, *, token: str | None = None, protocol: int = P.PROTOCOL):
        self.core, self.manifest, self.token, self.protocol = core, manifest, token or core.token, protocol
        self.inbox: list[Envelope] = []
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.handlers: dict[str, object] = {}        # type запроса ядра -> функция (body) -> значение
        self._reader: asyncio.Task | None = None
        self.close_code: int | None = None

    async def open(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.core.url, headers={"Authorization": f"Bearer {self.token}"})
        self._reader = asyncio.create_task(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                env = Envelope.loads(msg.data)
                self.inbox.append(env)
                fn = self.handlers.get(env.type)
                if fn is not None and P.is_request(env.type):
                    try:
                        value = fn(env.body)
                        if asyncio.iscoroutine(value):
                            value = await value
                        await self.send_env(P.result_ok(env.id, value))
                    except Exception as e:
                        await self.send_env(P.result_error(env.id, "internal", str(e)))
        self.close_code = self.ws.close_code

    async def send_env(self, env: Envelope):
        await self.ws.send_str(env.dumps())

    async def send(self, msg_type: str, body: dict | None = None, re: str | None = None) -> Envelope:
        env = Envelope(msg_type, body or {}, re=re)
        await self.send_env(env)
        return env

    async def hello(self) -> Envelope:
        """Открыть соединение, отправить hello и вернуть welcome или reject."""
        await self.open()
        hello = await self.send("hello", P.hello_body(self.manifest, "raw00001", 4242) | {"protocol": self.protocol})
        reply = await self.wait("welcome", "reject")
        assert reply.re == hello.id
        return reply

    async def wait(self, *types: str, timeout=3.0, where=None) -> Envelope:
        async def waiter():
            seen = 0
            while True:
                for env in self.inbox[seen:]:
                    if env.type in types and (where is None or where(env)):
                        return env
                seen = len(self.inbox)
                await asyncio.sleep(0.01)
        return await asyncio.wait_for(waiter(), timeout)

    def of(self, msg_type: str) -> list[Envelope]:
        return [e for e in self.inbox if e.type == msg_type]

    async def request(self, msg_type: str, body: dict | None = None, timeout=3.0):
        """Запрос к ядру: возвращает тело result."""
        env = await self.send(msg_type, body)
        reply = await self.wait("result", timeout=timeout, where=lambda e: e.re == env.id)
        return reply.body

    async def close(self):
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader, 2)
        if self.session is not None:
            await self.session.close()


def manifest(module_id="raw", **extra) -> dict:
    """Минимальный верный манифест для сырых модулей."""
    base = {"id": module_id, "title": module_id.title(), "description": "Тестовый модуль.", "version": "1.0.0",
            "subscribe": ["presence", "settings", "profile", "module", "battery"],
            "emits": [{"kind": f"{module_id}.ping", "description": "пинг", "default_urgency": 2}]}
    base.update(extra)
    return base


def tool(name, **extra) -> dict:
    spec = {"name": name, "description": f"Инструмент {name}.", "params": {}, "risk": "auto", "availability": "always"}
    spec.update(extra)
    return spec


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)
