"""Клиент голосового адаптера: подключение к движку голоса ядра по `/voice/ws` (docs/VOICE_ENGINE.md).

Адаптер (Discord, шлюз ПК, тестовая заглушка) делает четыре вещи: сообщает, кто в канале (`speaker_join`/`speaker_leave`),
шлёт звук каждого говорящего отдельно (`send_audio`, PCM 16 кГц моно), играет то, что прислал движок (`on_play`) и слушает
команды остановки (`on_stop`, `on_leave`). Остальное делает клиент: рукопожатие hello/welcome, разбор кадров, подтверждения
`play.ack`, автопереподключение с нарастающей паузой и повторное объявление говорящих после обрыва.

    client = VoiceAdapterClient(adapter="discord", session="guild-123:voice-1", echo_free=True, can_barge_in=True,
                                on_play=play, on_stop=stop)
    await client.start()
    idx = await client.speaker_join("u42", "Маша")
    await client.send_audio("u42", pcm_16k_mono)

`on_play(frames, uid)`: корутина, играет кадры из `frames` (асинхронный итератор кусков PCM по 640 байт) и возвращает, сколько
миллисекунд реально сыграно (None: столько, сколько пришло). `frames` кончается, когда движок прислал play.end или play.stop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .. import protocol as core_protocol
from . import protocol as V

logger = logging.getLogger("gits.voice.client")

ENV_VOICE_URL = "GITS_VOICE_URL"
CONNECT_TIMEOUT_S = 10.0
WELCOME_TIMEOUT_S = 10.0
SEND_TIMEOUT_S = 5.0
RECONNECT_DELAYS = (0.5, 1, 2, 4, 8, 15)


def default_url() -> str:
    """Адрес движка: GITS_VOICE_URL, иначе адрес шины ядра (GITS_CORE_URL) с путём /voice/ws."""
    explicit = os.environ.get(ENV_VOICE_URL, "").strip()
    if explicit:
        return explicit
    core = core_protocol.core_url()
    base = core.rsplit(core_protocol.WS_PATH, 1)[0] if core.endswith(core_protocol.WS_PATH) else core.rstrip("/")
    return base + V.WS_PATH


class PlayStream:
    """Кадры одной реплики: `async for pcm in stream` отдаёт куски по 640 байт, пока их присылает движок."""

    def __init__(self, uid: str, play_id: int, text: str, total_ms: int):
        self.uid, self.play_id, self.text, self.total_ms = uid, play_id, text, total_ms
        self.received_ms = 0
        self.stop_reason: str | None = None            # заполнено, если движок остановил реплику (play.stop)
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def _put(self, pcm: bytes) -> None:
        self.received_ms += len(pcm) * 1000 // (V.SAMPLE_RATE * V.SAMPLE_WIDTH)
        self._queue.put_nowait(pcm)

    def _close(self) -> None:
        self._queue.put_nowait(None)

    def drain(self) -> None:
        """Выбросить неиграные кадры (на play.stop адаптер не должен доигрывать очередь)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._close()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            self._queue.put_nowait(None)               # повторный вызов после конца тоже заканчивает
            raise StopAsyncIteration
        return item


@dataclass
class _Speaker:
    id: str
    name: str
    idx: int
    is_owner: bool = False
    is_bot: bool = False
    seq: int = 0


@dataclass
class AdapterStats:
    connects: int = 0
    frames_sent: int = 0
    frames_dropped: int = 0
    plays: int = 0
    errors: list[str] = field(default_factory=list)


class VoiceAdapterClient:
    def __init__(self, *, adapter: str, session: str, url: str | None = None, token: str | None = None,
                 echo_free: bool = False, can_barge_in: bool = False, speakers_labelled: bool = True, direct: bool = False,
                 on_play: Callable[[PlayStream, str], Awaitable[int | None]] | None = None,
                 on_stop: Callable[[str, str], Awaitable[None] | None] | None = None,
                 on_leave: Callable[[], Awaitable[None] | None] | None = None,
                 on_ready: Callable[[dict], Awaitable[None] | None] | None = None,
                 reconnect: bool = True, http_session: aiohttp.ClientSession | None = None):
        self.adapter, self.session_id = adapter, session
        self.url = url or default_url()
        self._token = token if token is not None else (core_protocol.env_token() or "")
        self.capabilities = {"echo_free": echo_free, "can_barge_in": can_barge_in,
                             "speakers_labelled": speakers_labelled, "direct": direct}
        self._on_play, self._on_stop, self._on_leave, self._on_ready = on_play, on_stop, on_leave, on_ready
        self._reconnect = reconnect
        self._http = http_session
        self._own_http = http_session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._send_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closing = False
        self._runner: asyncio.Task | None = None
        self._speakers: dict[str, _Speaker] = {}
        self._next_idx = 1
        self._plays: dict[str, PlayStream] = {}
        self._play_tasks: dict[str, asyncio.Task] = {}
        self._play_by_id: dict[int, PlayStream] = {}
        self.welcome: dict = {}
        self.stats = AdapterStats()
        self.last_error: str | None = None

    # ── жизненный цикл ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запустить соединение в фоне (сама переподключается). Дождаться готовности: `await wait_ready()`."""
        if self._runner is None:
            self._closing = False
            self._runner = asyncio.get_running_loop().create_task(self._run(), name=f"voice-adapter-{self.adapter}")

    async def wait_ready(self, timeout: float = CONNECT_TIMEOUT_S) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    async def stop(self) -> None:
        """Корректно закрыть: bye движку, остановить реплики, закрыть соединение. Повторный вызов безопасен."""
        self._closing = True
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await self._send_text(V.encode("bye", reason="adapter_stop"))
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                pass
            await ws.close()
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
            self._runner = None
        self._abort_plays("closed")
        if self._own_http and self._http is not None:
            await self._http.close()
            self._http = None
        self._ready.clear()

    # ── что адаптер сообщает движку ──────────────────────────────────────────────

    async def speaker_join(self, speaker_id: str, name: str = "", *, is_owner: bool = False, is_bot: bool = False) -> int:
        """Говорящий появился в канале. Возвращает его номер в бинарных кадрах (idx). Повторный вызов обновляет имя."""
        sp = self._speakers.get(speaker_id)
        if sp is None:
            sp = self._speakers[speaker_id] = _Speaker(speaker_id, name, self._next_idx, is_owner, is_bot)
            self._next_idx += 1
        else:
            sp.name, sp.is_owner, sp.is_bot = name or sp.name, is_owner, is_bot
        await self._announce(sp)
        return sp.idx

    async def speaker_leave(self, speaker_id: str) -> None:
        sp = self._speakers.pop(speaker_id, None)
        if sp is not None and self.ready:
            await self._send_text(V.encode("speaker.leave", id=speaker_id))

    async def speaking(self, speaker_id: str, active: bool) -> None:
        """Подсказка «начал/закончил говорить» (Discord даёт её сам); движок опирается на звук, подсказка только в состояние."""
        if speaker_id in self._speakers and self.ready:
            await self._send_text(V.encode("speaking.start" if active else "speaking.stop", id=speaker_id))

    async def send_audio(self, speaker_id: str, pcm16k: bytes) -> bool:
        """Звук говорящего (PCM s16le 16 кГц моно), любой длины; режется на кадры не длиннее 200 мс. False: звук потерян
        (нет связи или говорящий не объявлен): адаптер не копит очередь, речь в реальном времени не догоняют."""
        sp = self._speakers.get(speaker_id)
        if sp is None or not self.ready or self._ws is None:
            self.stats.frames_dropped += 1
            return False
        usable = len(pcm16k) - len(pcm16k) % V.SAMPLE_WIDTH
        try:
            for off in range(0, usable, V.MAX_PCM_BYTES):
                chunk = pcm16k[off:min(off + V.MAX_PCM_BYTES, usable)]
                await self._send_bytes(V.pack_frame(V.KIND_MIC, sp.idx, 0, sp.seq, chunk))
                sp.seq = (sp.seq + 1) & 0xFFFFFFFF
                self.stats.frames_sent += 1
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            self.stats.frames_dropped += 1
            return False
        return True

    async def ack(self, utterance_id: str, spoken_upto_ms: int, reason: str = "completed") -> None:
        """Ручное подтверждение воспроизведения (обычно его шлёт сам клиент после возврата on_play)."""
        if self.ready:
            await self._send_text(V.encode("play.ack", utterance_id=utterance_id, spoken_upto=max(0, int(spoken_upto_ms)), reason=reason))

    # ── соединение ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._session_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError, V.VoiceProtocolError) as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.warning("Голосовой адаптер %s: связь с движком потеряна (%s)", self.adapter, type(e).__name__)
            except Exception as e:                      # обработчик адаптера упал: не должно убивать связь навсегда
                self.last_error = f"{type(e).__name__}: {e}"
                logger.exception("Голосовой адаптер %s: непредвиденная ошибка", self.adapter)
            finally:
                self._ready.clear()
                self._abort_plays("error")
            if self._closing or not self._reconnect:
                break
            await asyncio.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
            attempt += 1

    async def _session_once(self) -> None:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        ws = await asyncio.wait_for(self._http.ws_connect(self.url, headers=headers, heartbeat=10,
                                                          max_msg_size=V.MAX_WS_MESSAGE_BYTES), CONNECT_TIMEOUT_S)
        self._ws = ws
        try:
            await self._send_text(V.encode("hello", version=V.VOICE_PROTOCOL, adapter=self.adapter, session=self.session_id,
                                           capabilities=self.capabilities, format=V.audio_format()))
            msg = await asyncio.wait_for(ws.receive(), WELCOME_TIMEOUT_S)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise V.VoiceProtocolError("ожидался welcome")
            welcome = V.decode(msg.data, allowed=V.TO_ADAPTER)
            if welcome["type"] == "error":
                self.last_error = f"{welcome.get('code')}: {welcome.get('message', '')}"
                self._closing = self._closing or welcome.get("code") in ("protocol", "unauthorized")
                raise V.VoiceProtocolError(self.last_error)
            if welcome["type"] != "welcome":
                raise V.VoiceProtocolError("ожидался welcome")
            self.welcome = welcome
            self.stats.connects += 1
            for sp in list(self._speakers.values()):    # после обрыва движок о говорящих не помнит
                await self._announce(sp, force=True)
            self._ready.set()
            if self._on_ready is not None:
                await _maybe_await(self._on_ready(welcome))
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    self._on_binary(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
        finally:
            self._ready.clear()
            if not ws.closed:
                await ws.close()
            self._ws = None

    async def _announce(self, sp: _Speaker, force: bool = False) -> None:
        if self.ready or force:
            await self._send_text(V.encode("speaker.join", id=sp.id, name=sp.name or None, idx=sp.idx,
                                           is_owner=sp.is_owner, is_bot=sp.is_bot))

    async def _send_text(self, text: str) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("нет соединения с движком голоса")
        async with self._send_lock:
            await asyncio.wait_for(ws.send_str(text), SEND_TIMEOUT_S)

    async def _send_bytes(self, data: bytes) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("нет соединения с движком голоса")
        async with self._send_lock:
            await asyncio.wait_for(ws.send_bytes(data), SEND_TIMEOUT_S)

    # ── что присылает движок ────────────────────────────────────────────────────

    async def _on_text(self, text: str) -> None:
        try:
            msg = V.decode(text, allowed=V.TO_ADAPTER)
        except V.VoiceProtocolError as e:
            self.stats.errors.append(str(e))
            logger.warning("Голосовой адаптер %s: сообщение движка отброшено (%s)", self.adapter, e)
            return
        kind = msg["type"]
        if kind == "play.start":
            self._begin_play(msg)
        elif kind == "play.end":
            stream = self._plays.get(msg["utterance_id"])
            if stream is not None:
                stream._close()
        elif kind == "play.stop":
            await self._stop_play(msg.get("utterance_id"), msg["reason"])
        elif kind == "leave":
            if self._on_leave is not None:
                await _maybe_await(self._on_leave())
        elif kind == "error":
            self.last_error = f"{msg.get('code')}: {msg.get('message', '')}"
            self.stats.errors.append(self.last_error)
        elif kind == "bye":
            self._closing = True
            if self._ws is not None:
                await self._ws.close()

    def _begin_play(self, msg: dict) -> None:
        uid = msg["utterance_id"]
        stream = PlayStream(uid, msg["play_id"], msg.get("text", ""), int(msg.get("total_ms") or 0))
        self._plays[uid] = stream
        self._play_by_id[stream.play_id] = stream
        self.stats.plays += 1
        self._play_tasks[uid] = asyncio.get_running_loop().create_task(self._play(stream), name=f"voice-play-{uid}")

    def _on_binary(self, data: bytes) -> None:
        try:
            frame = V.unpack_frame(data)
        except V.VoiceProtocolError as e:
            self.stats.errors.append(str(e))
            return
        if frame.kind != V.KIND_PLAY:
            return
        stream = self._play_by_id.get(frame.stream)
        if stream is not None and stream.stop_reason is None:
            stream._put(frame.pcm)

    async def _play(self, stream: PlayStream) -> None:
        """Играет реплику через on_play и подтверждает её движку: сколько мс реально сыграно и почему кончилось."""
        spoken: int | None = None
        try:
            if self._on_play is not None:
                spoken = await self._on_play(stream, stream.uid)
            else:                                        # без обработчика кадры просто принимаются (адаптер-«наблюдатель»)
                async for _ in stream:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Голосовой адаптер %s: ошибка воспроизведения", self.adapter)
            await self._ack_safe(stream, spoken if spoken is not None else stream.received_ms, "error")
            return
        finally:
            self._plays.pop(stream.uid, None)
            self._play_by_id.pop(stream.play_id, None)
            self._play_tasks.pop(stream.uid, None)
        reason = stream.stop_reason or "completed"
        await self._ack_safe(stream, spoken if spoken is not None else stream.received_ms, reason)

    async def _ack_safe(self, stream: PlayStream, ms: int, reason: str) -> None:
        try:
            await self.ack(stream.uid, ms, reason)
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            pass

    async def _stop_play(self, uid: str | None, reason: str) -> None:
        targets = [self._plays[uid]] if uid in self._plays else ([] if uid else list(self._plays.values()))
        for stream in targets:
            stream.stop_reason = reason
            stream.drain()
            if self._on_stop is not None:
                await _maybe_await(self._on_stop(stream.uid, reason))

    def _abort_plays(self, reason: str) -> None:
        for stream in list(self._plays.values()):
            stream.stop_reason = stream.stop_reason or reason
            stream.drain()
        for task in list(self._play_tasks.values()):
            task.cancel()
        self._plays.clear()
        self._play_by_id.clear()
        self._play_tasks.clear()


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value
