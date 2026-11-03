"""Один раз войти в Twitch. Запускается ВРУЧНУЮ в терминале, в браузере входишь сам, ассистентка пароля и кода не видит:

    python modules/twitch/tw_login.py bot            # под аккаунтом ГЛаДОС (чат, модерация)
    python modules/twitch/tw_login.py broadcaster    # под аккаунтом владельца канала (события канала, название, клипы)

Если бот и канал один аккаунт, достаточно `broadcaster` (бот возьмёт его токен). Нужны TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET
в agent/secrets.env (dev.twitch.tv/console, Redirect URL приложения: http://localhost:3000). Токены сохраняются в папке памяти
(MEMORY_DIR, по умолчанию agent/memory) в twitch_tokens.json.
"""
import asyncio
import os
import secrets
import sys
import urllib.parse
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent / "sdk")]

from common import default_memory_dir, secrets_file  # noqa: E402

try:
    from dotenv import load_dotenv
    load_dotenv(secrets_file())
except ImportError:
    pass

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from twitch_api import Tokens  # noqa: E402

PORT = 3000
REDIRECT = f"http://localhost:{PORT}"
SCOPES = {
    "bot": ["user:read:chat", "user:write:chat", "user:bot", "moderator:manage:banned_users", "moderator:manage:chat_messages",
            "moderator:manage:announcements"],
    "broadcaster": ["user:read:chat", "user:write:chat", "channel:bot", "channel:manage:broadcast", "clips:edit",
                    "moderator:read:followers", "channel:read:subscriptions", "bits:read", "channel:read:redemptions"],
}


async def wait_for_code(state: str) -> str:
    got: asyncio.Future = asyncio.get_running_loop().create_future()

    async def handler(request: web.Request) -> web.Response:
        if request.query.get("state") == state and request.query.get("code") and not got.done():
            got.set_result(request.query["code"])
            return web.Response(text="Готово, можно закрыть вкладку.")
        if request.query.get("error") and not got.done():
            got.set_exception(RuntimeError(request.query.get("error_description") or request.query["error"]))
        return web.Response(text="Ожидаю вход...")

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    try:
        return await asyncio.wait_for(got, 300)
    finally:
        await runner.cleanup()


async def main(role: str) -> None:
    client_id, client_secret = os.getenv("TWITCH_CLIENT_ID", "").strip(), os.getenv("TWITCH_CLIENT_SECRET", "").strip()
    if role not in SCOPES or not (client_id and client_secret):
        print("Использование: tw_login.py bot|broadcaster. В agent/secrets.env нужны TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET.")
        return
    state = secrets.token_urlsafe(16)
    url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(SCOPES[role]),
        "state": state, "force_verify": "true"})
    print(f"Открываю браузер. Войди под аккаунтом «{role}» и разреши доступ. Если окно не открылось, адрес:\n{url}")
    webbrowser.open(url)
    code = await wait_for_code(state)
    async with aiohttp.ClientSession() as http:
        async with http.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": client_id, "client_secret": client_secret, "code": code, "grant_type": "authorization_code",
                "redirect_uri": REDIRECT}) as resp:
            tok = await resp.json()
        if "access_token" not in tok:
            print("Twitch не выдал токен:", tok.get("message") or tok.get("error"))
            return
        async with http.get("https://api.twitch.tv/helix/users", headers={
                "Authorization": f"Bearer {tok['access_token']}", "Client-Id": client_id}) as resp:
            me = (await resp.json())["data"][0]
    import time
    entry = {"access": tok["access_token"], "refresh": tok["refresh_token"], "expires_at": time.time() + float(tok.get("expires_in", 3600)),
             "user_id": me["id"], "login": me["login"], "scopes": tok.get("scope") or SCOPES[role]}
    tokens = Tokens(default_memory_dir())
    tokens.put(role, entry)
    if role == "broadcaster" and not tokens.get("bot"):
        tokens.put("bot", dict(entry))                    # один аккаунт на оба дела
    print(f"Готово: вход выполнен как {me['display_name']} ({role}). Токены сохранены в {tokens.path}.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
