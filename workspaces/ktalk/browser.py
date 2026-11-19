import asyncio
import binascii
import re
from base64 import b64decode
from pathlib import Path
from urllib.parse import urlsplit

from workspaces.telemost.browser import BrowserCall

WAITING = re.compile(r"ожидайте допуска|ожидаем подтверждения|комнат[аеу] ожидания|модератор.*допустит", re.I)
BLOCKED = re.compile(r"доступ для внешних участников отключен|войдите.*пользовател[ье]м пространства", re.I)
CAPTURE_SCRIPT = Path(__file__).with_name("receivers.js")
GUEST_NAME = "ГЛаДОС"
NAME_MAX = 64
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class KTalkBrowser(BrowserCall):
    def __init__(self):
        super().__init__()
        self.incoming = asyncio.Queue(maxsize=192)
        self.names = {}                      # дорожка -> имя участника, найденное на странице
        self.guest_named = False

    async def open(self, url):
        self.origin = f"https://{urlsplit(url).hostname}"

        async def setup(page):
            await page.expose_binding("gitsKTalkAudio", self.receive)
            await page.expose_binding("gitsKTalkName", self.receive_name)
            await page.add_init_script(path=CAPTURE_SCRIPT)

        await self.open_page(self.origin, url, setup=setup, extra_args=("--autoplay-policy=no-user-gesture-required",))

    def trusted(self, source, speaker):
        if source["frame"] != self.page.main_frame or not re.fullmatch(r"[1-9][0-9]{0,7}", str(speaker)):
            return False
        url = urlsplit(source["frame"].url)
        return f"{url.scheme}://{url.netloc}" == self.origin

    def receive_name(self, source, speaker, name):
        if not self.trusted(source, speaker) or not isinstance(name, str):
            return
        name = _CONTROL.sub("", name).strip()[:NAME_MAX]
        if name:
            self.names[str(speaker)] = name

    def receive(self, source, speaker, payload):
        if not self.trusted(source, speaker):
            return
        pcm = None
        if payload is not None:
            if not isinstance(payload, str) or len(payload) > 2732:
                return
            try:
                pcm = b64decode(payload, validate=True)
            except (ValueError, binascii.Error):
                return
            if len(pcm) != 2048:
                return
        if self.incoming.full():
            self.incoming.get_nowait()
        self.incoming.put_nowait((str(speaker), pcm))

    async def poll(self):
        page = self.page
        if page.is_closed():
            return "ended"
        leave = page.get_by_role("button", name="Покинуть комнату", exact=True).filter(visible=True).first
        if await leave.is_visible():
            if not self.connected:
                await asyncio.wait_for(page.evaluate(f"window.startGitsKTalkCapture({GUEST_NAME!r})"), 5)
                mic = page.get_by_role("button", name="Включить микрофон", exact=True).filter(visible=True).first
                if await mic.is_visible() and await mic.is_enabled():
                    await mic.click(timeout=2000)
                self.connected = True
            return "connected"
        if self.connected:
            return "ended"
        if await page.get_by_text(BLOCKED).first.is_visible():
            return "auth_required"
        name = page.get_by_placeholder(re.compile("например", re.I)).first
        if not self.guest_named and await name.is_visible():
            await name.fill(GUEST_NAME)
            await page.get_by_role("button", name="Продолжить", exact=True).click(timeout=3000)
            self.guest_named = True
        join = page.get_by_role("button", name="Присоединиться", exact=True).filter(visible=True).first
        if not self.join_clicked and await join.is_visible() and await join.is_enabled():
            for label in ("Выключить камеру", "Выключить микрофон"):
                control = page.get_by_role("button", name=re.compile("^" + label, re.I)).filter(visible=True).first
                if await control.is_visible():
                    await control.click(timeout=2000)
            await join.click(timeout=3000)
            self.join_clicked = True
        return "waiting" if await page.get_by_text(WAITING).first.is_visible() else "connecting"
