"""Клиент Twitch Helix и токены (два входа: `bot` - аккаунт ГЛаДОС, `broadcaster` - владелец канала, необязателен).

Токены лежат в MEMORY_DIR/twitch_tokens.json (их пишет `tw_login.py`, обновляет сам клиент: refresh_token у Twitch меняется
при каждом обновлении). Client ID и секрет приложения читаются из окружения (agent/secrets.env). Сеть вынесена в `_request`:
тесты подменяют её. Секреты в журнал и тексты ошибок не попадают (`scrub`).
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp

from common import scrub

log = logging.getLogger("twitch.api")

HELIX = "https://api.twitch.tv/helix"
OAUTH = "https://id.twitch.tv/oauth2/token"
REFRESH_MARGIN_S = 120.0


class TwitchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Twitch {status}: {message}")
        self.status, self.message = status, message


class Tokens:
    """Файл токенов: {роль: {access, refresh, expires_at, user_id, login, scopes}}. Записывается целиком и атомарно."""

    def __init__(self, memory_dir: str):
        self.path = Path(memory_dir) / "twitch_tokens.json"
        self.data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, role: str) -> dict | None:
        return self.data.get(role)

    def put(self, role: str, entry: dict) -> None:
        self.data[role] = entry
        self.save()


class Helix:
    def __init__(self, tokens: Tokens, client_id: str, client_secret: str):
        self.tokens, self.client_id, self.client_secret = tokens, client_id, client_secret
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def has_role(self, role: str) -> bool:
        return bool(self.tokens.get(role))

    def user_id(self, role: str) -> str:
        return str((self.tokens.get(role) or {}).get("user_id") or "")

    def login(self, role: str) -> str:
        return str((self.tokens.get(role) or {}).get("login") or "")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ── сеть (подменяется в тестах) ──────────────────────────────────────────

    async def _request(self, method: str, url: str, *, headers=None, params=None, json_body=None, data=None):
        """(status, json | None)."""
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        async with self._session.request(method, url, headers=headers, params=params, json=json_body, data=data) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            return resp.status, body

    # ── токены ───────────────────────────────────────────────────────────────

    async def token(self, role: str, *, force: bool = False) -> str:
        async with self._lock:
            entry = self.tokens.get(role)
            if not entry:
                raise TwitchError(401, f"вход «{role}» не выполнен: запусти tw_login.py")
            if force or float(entry.get("expires_at") or 0) - time.time() < REFRESH_MARGIN_S:
                await self._refresh(role, entry)
            return self.tokens.get(role)["access"]

    async def _refresh(self, role: str, entry: dict) -> None:
        status, body = await self._request("POST", OAUTH, data={
            "grant_type": "refresh_token", "refresh_token": entry.get("refresh", ""),
            "client_id": self.client_id, "client_secret": self.client_secret})
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise TwitchError(status, "не удалось обновить токен: войди заново через tw_login.py")
        self.tokens.put(role, {**entry, "access": body["access_token"],
                               "refresh": body.get("refresh_token", entry.get("refresh")),
                               "expires_at": time.time() + float(body.get("expires_in") or 3600)})

    # ── Helix ────────────────────────────────────────────────────────────────

    async def call(self, role: str, method: str, path: str, *, params=None, json_body=None):
        """Вызов Helix от имени роли; один повтор после обновления токена при 401. Возвращает json (или {})."""
        for attempt in (0, 1):
            tok = await self.token(role, force=bool(attempt))
            headers = {"Authorization": f"Bearer {tok}", "Client-Id": self.client_id}
            try:
                status, body = await self._request(method, f"{HELIX}{path}", headers=headers, params=params,
                                                   json_body=json_body)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise TwitchError(0, scrub(f"сеть: {e}", tok, self.client_secret)) from e
            if status == 401 and attempt == 0:
                continue
            if status >= 400:
                msg = (body or {}).get("message") if isinstance(body, dict) else ""
                raise TwitchError(status, scrub(str(msg or "ошибка"), tok, self.client_secret))
            return body if isinstance(body, dict) else {}
        raise TwitchError(401, "токен не принят")

    async def send_chat(self, channel_id: str, text: str, reply_to: str = "") -> dict:
        body = {"broadcaster_id": channel_id, "sender_id": self.user_id("bot"), "message": text}
        if reply_to:
            body["reply_parent_message_id"] = reply_to
        res = await self.call("bot", "POST", "/chat/messages", json_body=body)
        item = (res.get("data") or [{}])[0]
        if item.get("is_sent") is False:
            raise TwitchError(422, str((item.get("drop_reason") or {}).get("message") or "сообщение отклонено"))
        return item

    async def announce(self, channel_id: str, text: str) -> None:
        await self.call("bot", "POST", "/chat/announcements",
                        params={"broadcaster_id": channel_id, "moderator_id": self.user_id("bot")},
                        json_body={"message": text})

    async def stream(self, channel_id: str) -> dict | None:
        res = await self.call("bot", "GET", "/streams", params={"user_id": channel_id})
        data = res.get("data") or []
        return data[0] if data else None

    async def channel_info(self, channel_id: str) -> dict:
        res = await self.call("bot", "GET", "/channels", params={"broadcaster_id": channel_id})
        return (res.get("data") or [{}])[0]

    async def set_channel(self, channel_id: str, title: str = "", game: str = "") -> dict:
        body: dict = {}
        if title:
            body["title"] = title
        if game:
            found = (await self.call("bot", "GET", "/games", params={"name": game})).get("data") or []
            if not found:
                raise TwitchError(404, f"игра «{game}» не найдена на Twitch")
            body["game_id"] = found[0]["id"]
        if not body:
            raise TwitchError(400, "нужно название или игра")
        await self.call("broadcaster", "PATCH", "/channels", params={"broadcaster_id": channel_id}, json_body=body)
        return body

    async def clip(self, channel_id: str) -> dict:
        role = "broadcaster" if self.has_role("broadcaster") else "bot"
        res = await self.call(role, "POST", "/clips", params={"broadcaster_id": channel_id})
        return (res.get("data") or [{}])[0]

    async def lookup(self, login: str) -> dict:
        res = await self.call("bot", "GET", "/users", params={"login": login.lstrip("@").lower()})
        data = res.get("data") or []
        if not data:
            raise TwitchError(404, f"пользователь «{login}» не найден")
        return data[0]

    async def ban(self, channel_id: str, user_id: str, duration: int = 0, reason: str = "") -> None:
        data = {"user_id": user_id, "reason": reason[:500]}
        if duration > 0:
            data["duration"] = int(duration)
        await self.call("bot", "POST", "/moderation/bans",
                        params={"broadcaster_id": channel_id, "moderator_id": self.user_id("bot")}, json_body={"data": data})

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self.call("bot", "DELETE", "/moderation/chat",
                        params={"broadcaster_id": channel_id, "moderator_id": self.user_id("bot"),
                                "message_id": message_id})

    async def subscribe(self, role: str, sub_type: str, version: str, condition: dict, session_id: str) -> None:
        await self.call(role, "POST", "/eventsub/subscriptions", json_body={
            "type": sub_type, "version": version, "condition": condition,
            "transport": {"method": "websocket", "session_id": session_id}})
