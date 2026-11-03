"""EventSub по WebSocket: одна сессия на одну роль (токен). Публичный адрес не нужен.

`EventSubSession.run()` держит соединение: после `session_welcome` создаёт подписки, на `notification` вызывает `on_event(type,
event)`, на `session_reconnect` переходит на новый адрес, при обрыве переподключается с задержкой. Разбор сообщений вынесен в
`handle` (тесты подают сообщения напрямую), открытие соединения в `_open` (тесты подменяют).
"""
import asyncio
import logging

import aiohttp

from common import scrub
from twitch_api import TwitchError

log = logging.getLogger("twitch.eventsub")

URL = "wss://eventsub.wss.twitch.tv/ws"
RETRY_S = (2.0, 5.0, 15.0, 30.0)


class EventSubSession:
    def __init__(self, helix, role: str, subs: list[tuple[str, str, dict]], on_event, url: str = URL):
        """subs: [(тип, версия, условие)]; on_event: async (тип, событие) -> None."""
        self.helix, self.role, self.subs, self.on_event, self.url = helix, role, subs, on_event, url
        self.session_id = ""
        self.connected = False
        self.failed: list[str] = []          # типы подписок, которые Twitch не принял (не хватает прав)
        self._reconnect_url = ""

    async def handle(self, msg: dict) -> None:
        meta = msg.get("metadata") or {}
        kind, payload = meta.get("message_type"), msg.get("payload") or {}
        if kind == "session_welcome":
            self.session_id = str((payload.get("session") or {}).get("id") or "")
            self.connected = True
            if not self._reconnect_url:                      # при переходе на новый адрес подписки уже перенесены
                await self._subscribe()
            self._reconnect_url = ""
        elif kind == "notification":
            sub_type = str((payload.get("subscription") or {}).get("type") or meta.get("subscription_type") or "")
            await self.on_event(sub_type, payload.get("event") or {})
        elif kind == "session_reconnect":
            self._reconnect_url = str((payload.get("session") or {}).get("reconnect_url") or "")
        elif kind == "revocation":
            log.warning("подписка отозвана: %s", (payload.get("subscription") or {}).get("type"))

    async def _subscribe(self) -> None:
        self.failed = []
        for sub_type, version, condition in self.subs:
            try:
                await self.helix.subscribe(self.role, sub_type, version, condition, self.session_id)
            except TwitchError as e:
                self.failed.append(sub_type)
                log.warning("подписка %s не создана (%s): %s", sub_type, self.role, scrub(e.message))

    async def _open(self, url: str):
        """Асинхронный генератор сообщений-словарей одного соединения."""
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url, heartbeat=30) as ws:
                async for raw in ws:
                    if raw.type == aiohttp.WSMsgType.TEXT:
                        yield raw.json()
                    elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        return

    async def run(self) -> None:
        tries = 0
        while True:
            url = self._reconnect_url or self.url
            try:
                async for msg in self._open(url):
                    tries = 0
                    await self.handle(msg)
                    if self._reconnect_url:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("EventSub (%s) оборвался: %s", self.role, scrub(str(e)))
            self.connected = False
            if self._reconnect_url:
                continue
            await asyncio.sleep(RETRY_S[min(tries, len(RETRY_S) - 1)])
            tries += 1
