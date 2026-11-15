"""WebSocket-вход движка голоса: `/voice/ws` на HTTP-сервере ядра (порт дашборда) и `GET /api/voice` (состояние для дашборда).

Один сокет это одна голосовая сессия адаптера. Порядок: токен шины в заголовке `Authorization: Bearer` (тот же, что у `/ws`
модулей), затем `hello`, в ответ `welcome`, дальше управление и звук (docs/VOICE_ENGINE.md, «Протокол»). Нарушения протокола
получают сообщение `error`; поток, который шлёт мусор снова и снова, закрывается. Обрыв соединения закрывает сессию.
"""
import asyncio
import hmac
import logging
from typing import Callable

from aiohttp import WSMsgType, web

from . import protocol as V
from .engine import VoiceEngine
from .session import Capabilities, SessionLimitError

logger = logging.getLogger("voice_engine.server")

HELLO_TIMEOUT_S = 10.0
SEND_TIMEOUT_S = 5.0
MAX_PROTOCOL_ERRORS = 20                 # после стольких нарушений соединение закрывается


class WsTransport:
    """Канал к адаптеру поверх WebSocket: отправки не пересекаются (замок) и не висят дольше SEND_TIMEOUT_S."""

    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self.ws.closed

    async def _send(self, coro_factory) -> None:
        if self.ws.closed:
            raise ConnectionError("соединение с адаптером закрыто")
        try:
            async with self._lock:
                await asyncio.wait_for(coro_factory(), SEND_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise ConnectionError("адаптер не принимает данные") from None

    async def send_control(self, text: str) -> None:
        await self._send(lambda: self.ws.send_str(text))

    async def send_play_frame(self, play_id: int, seq: int, pcm: bytes) -> None:
        data = V.pack_frame(V.KIND_PLAY, 0, play_id, seq, pcm)
        await self._send(lambda: self.ws.send_bytes(data))

    async def close(self, reason: str) -> None:
        if not self.ws.closed:
            await self.ws.close(code=1000, message=reason.encode("utf-8")[:100])


class VoiceServer:
    def __init__(self, engine: VoiceEngine, token: Callable[[], str], allow: Callable[[str | None], bool] = lambda remote: True,
                 hello_timeout_s: float = HELLO_TIMEOUT_S):
        self.engine = engine
        self._token = token
        self._allow = allow
        self.hello_timeout_s = hello_timeout_s
        self.connections = 0
        self.rejected = 0

    def attach(self, app: web.Application) -> None:
        app.router.add_get(V.WS_PATH, self.handle)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not self._allow(request.remote):
            self.rejected += 1
            logger.warning("Голосовой адаптер не с разрешённого адреса отклонён")
            raise web.HTTPForbidden()
        given = request.headers.get("Authorization", "")
        if not hmac.compare_digest(given.encode("utf-8"), f"Bearer {self._token()}".encode("utf-8")):
            self.rejected += 1
            logger.warning("Голосовой адаптер с неверным токеном отклонён")
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=V.MAX_WS_MESSAGE_BYTES)
        await ws.prepare(request)
        transport = WsTransport(ws)
        session_id = await self._handshake(ws, transport)
        if session_id is None:
            return ws
        self.connections += 1
        reason = "disconnect"
        try:
            reason = await self._serve(ws, transport, session_id)
        finally:
            await self.engine.close_session(session_id, reason, transport=transport)
            if not ws.closed:
                await ws.close()
        return ws

    async def _reject(self, transport: WsTransport, code: str, message: str) -> None:
        self.rejected += 1
        logger.warning("Голосовой адаптер отклонён: %s", message)
        try:
            await transport.send_control(V.encode("error", code=code, message=message))
        except ConnectionError:
            pass
        await transport.close(code)

    async def _handshake(self, ws: web.WebSocketResponse, transport: WsTransport) -> str | None:
        try:
            msg = await ws.receive(timeout=self.hello_timeout_s)
        except asyncio.TimeoutError:
            await self._reject(transport, "timeout", "hello не пришёл вовремя")
            return None
        if msg.type != WSMsgType.TEXT:
            await self._reject(transport, "protocol", "первым сообщением должен быть текстовый hello")
            return None
        try:
            hello = V.decode(msg.data, allowed=V.TO_ENGINE)
            if hello["type"] != "hello":
                raise V.VoiceProtocolError("до welcome допустим только hello")
        except V.VoiceProtocolError as e:
            await self._reject(transport, "protocol", str(e))
            return None
        caps = Capabilities(**hello["capabilities"])
        try:
            sess = await self.engine.open_session(hello["session"], hello["adapter"], transport, caps)
        except SessionLimitError as e:
            await self._reject(transport, "limit", str(e))
            return None
        try:
            await transport.send_control(V.encode(
                "welcome", version=V.VOICE_PROTOCOL, session=sess.id, format=V.audio_format(),
                settings={"barge_in": sess.barge_in_policy, "barge_in_ms": sess.barge_in_ms, "lang": self.engine.config.lang},
                limits={"max_speakers": self.engine.sessions_manager.max_speakers, "max_pcm_bytes": V.MAX_PCM_BYTES}))
        except ConnectionError:
            await self.engine.close_session(sess.id, "disconnect", transport=transport)
            return None
        return sess.id

    async def _serve(self, ws: web.WebSocketResponse, transport: WsTransport, session_id: str) -> str:
        errors = 0

        async def complain(code: str, message: str) -> bool:
            """Сообщить адаптеру о нарушении; True, если пора закрывать соединение."""
            nonlocal errors
            errors += 1
            try:
                await transport.send_control(V.encode("error", code=code, message=message))
            except ConnectionError:
                return True
            return errors > MAX_PROTOCOL_ERRORS

        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    frame = V.unpack_frame(msg.data)
                except V.VoiceProtocolError as e:
                    if await complain("protocol", str(e)):
                        return "protocol"
                    continue
                if frame.kind != V.KIND_MIC:
                    if await complain("protocol", "адаптер шлёт только кадры звука говорящих"):
                        return "protocol"
                    continue
                self.engine.feed(session_id, frame.speaker, frame.pcm)
            elif msg.type == WSMsgType.TEXT:
                try:
                    body = V.decode(msg.data, allowed=V.TO_ENGINE)
                except V.VoiceProtocolError as e:
                    if await complain("protocol", str(e)):
                        return "protocol"
                    continue
                outcome = await self._route(session_id, body)
                if outcome == "bye":
                    return "bye"
                if outcome and await complain("rejected", outcome):
                    return "protocol"
            elif msg.type == WSMsgType.ERROR:
                return "error"
        return "disconnect"

    async def _route(self, session_id: str, body: dict) -> str | None:
        """Обработать сообщение управления. Возвращает «bye», текст отказа или None."""
        kind, engine = body["type"], self.engine
        try:
            if kind == "speaker.join":
                engine.add_speaker(session_id, body["id"], body.get("name") or body["id"], body["idx"],
                                   is_owner=bool(body.get("is_owner")), is_bot=bool(body.get("is_bot")))
            elif kind == "speaker.leave":
                engine.remove_speaker(session_id, body["id"])
            elif kind in ("speaking.start", "speaking.stop"):
                engine.hint_speaking(session_id, body["id"], kind == "speaking.start")
            elif kind == "play.ack":
                engine.ack(session_id, body["utterance_id"], body["spoken_upto"], body["reason"])
            elif kind == "bye":
                return "bye"
            else:
                return f"сообщение «{kind}» после welcome не допускается"
        except (SessionLimitError, ValueError, KeyError) as e:
            return str(e)
        return None

