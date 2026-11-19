import asyncio
import logging
import re
import sys
from pathlib import Path
from uuid import uuid4

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.extend([str(ROOT), str(ROOT / "agent" / "interfaces" / "workspace")])

from gits_sdk import ToolError, WorkspaceClient  # noqa: E402
from gits_sdk.voice import VoiceAdapterClient  # noqa: E402

from workspaces.telemost.browser import JOIN_TIMEOUT, BrowserCall, browser_available  # noqa: E402

log = logging.getLogger("gits.telemost")
LABEL = "Яндекс Телемост"
SPEAKER = "meeting"
MEETING_LINK = re.compile(r"https://telemost\.yandex\.(?:ru|com)/j/([0-9]{1,32})/?(?:[?#][^\s]*)?", re.I)
MANIFEST = {
    "id": "telemost",
    "role": "workspace",
    "ownership": "owned",
    "title": LABEL,
    "description": "Голосовые встречи по ссылке Яндекс Телемоста через браузер.",
    "version": "1.0.0",
    "state": True,
    "panel": {"title": LABEL, "icon": "mic", "order": 62, "actions": ["join", "leave"]},
    "tools": [
        {
            "name": "voice_join",
            "description": "Присоединиться к созвону Яндекс Телемоста по ссылке, которую передал пользователь. "
                           "Вызывай по явной просьбе подключиться. Для голоса во встрече используй voice.say.",
            "params": {"link": {"type": "string", "required": True, "description": "Ссылка https://telemost.yandex.ru/j/…"}},
            "keywords": ["телемост", "telemost", "созвон", "подключись"],
            "risk": "ask",
            "availability": "always",
            "informative": True,
        },
        {
            "name": "voice_leave",
            "description": "Выйти из текущего созвона Яндекс Телемоста.",
            "params": {},
            "keywords": ["телемост", "telemost", "созвон", "отключись", "выйди"],
            "risk": "auto",
            "availability": "always",
            "informative": True,
        },
    ],
}


def meeting_id(link):
    match = MEETING_LINK.fullmatch(link.strip()) if isinstance(link, str) else None
    if match is None:
        raise ToolError("Нужна ссылка https://telemost.yandex.ru/j/…", code="bad_request")
    return match[1]


class Telemost:
    def __init__(self, client, browser_factory=BrowserCall, voice_factory=VoiceAdapterClient):
        self.client = client
        self.browser_factory = browser_factory
        self.voice_factory = voice_factory
        self.available = browser_factory is not BrowserCall or browser_available()
        self.task = None
        self.stop_requested = asyncio.Event()
        self.status = "Нет активного звонка" if self.available else "Для браузерного входа нужны Chromium, Playwright и PulseAudio (Linux)"
        self.changed = asyncio.Event()
        client.tool("voice_join")(self.join_from_dialog)
        client.tool("voice_leave")(self.leave)
        client.panel_action("join")(self.join_from_panel)
        client.panel_action("leave")(self.leave_from_panel)
        client.on_disconnect(self.disconnect)

    async def join_from_dialog(self, args):
        return self.join(args.get("link"))

    async def join_from_panel(self, args):
        return {"ok": True, "message": self.join(args.get("value"))}

    async def leave_from_panel(self, args):
        return {"ok": True, "message": await self.leave(args)}

    def join(self, link):
        number = meeting_id(link)
        if not self.available:
            raise ToolError(self.status, code="unavailable")
        if self.task is not None and not self.task.done():
            raise ToolError("Сначала отключитесь от текущей встречи", code="busy")
        self.set_status("Подключение к встрече…")
        self.stop_requested.clear()
        self.task = asyncio.create_task(self.run_call(number))
        return self.status

    async def disconnect(self, *_):
        task = self.task
        if task is not None and not task.done():
            self.stop_requested.set()
            await asyncio.shield(task)
        if self.task is task:
            self.task = None

    async def leave(self, _args):
        await self.disconnect()
        return self.status

    def set_status(self, text):
        self.status = text
        self.changed.set()

    async def publish(self):
        while True:
            await self.client.wait_connected()
            self.changed.clear()
            await self.client.state(self.status, {"in_call": self.task is not None and not self.task.done()})
            await self.client.panel.set([
                {"type": "text", "title": "Состояние", "text": self.status},
                {"type": "input", "label": "Ссылка на встречу", "placeholder": "https://telemost.yandex.ru/j/…",
                 "submit": "Подключиться", "action": "join"},
                {"type": "buttons", "items": [{"label": "Отключиться", "action": "leave"}]},
            ])
            try:
                await asyncio.wait_for(self.changed.wait(), 5)
            except TimeoutError:
                pass

    async def run_call(self, number):
        browser = self.browser_factory()

        connected = False

        async def play(frames, uid):
            if connected and not self.stop_requested.is_set():
                return await browser.play(frames, uid)
            return 0

        async def stop_playback(_uid, _reason):
            browser.stop_playback()

        async def request_leave():
            self.stop_requested.set()

        voice = self.voice_factory(
            adapter="telemost", session=f"telemost:{uuid4().hex}", title=LABEL, speakers_labelled=False,
            echo_free=False, can_barge_in=False, on_play=play, on_stop=stop_playback, on_leave=request_leave,
        )
        result = "Нет активного звонка"
        try:
            await voice.start()
            if not await voice.wait_ready(10):
                result = "Голосовой движок GitS недоступен"
                return
            if self.stop_requested.is_set():
                return
            await voice.speaker_join(SPEAKER, "Участники Телемоста")
            await browser.open(number)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + JOIN_TIMEOUT
            while not self.stop_requested.is_set():
                state = await browser.poll()
                if state == "auth_required":
                    result = "Телемост просит войти в аккаунт для этой встречи"
                    break
                if state == "ended":
                    if not connected:
                        result = "Браузер закрылся до входа во встречу"
                    break
                if state == "connected":
                    if not connected:
                        connected = True
                        self.set_status("В звонке через браузер")
                    await self.forward_audio(browser, voice)
                elif loop.time() >= deadline:
                    result = "Вход во встречу не подтверждён"
                    break
                elif state == "waiting" and self.status != "Ожидает допуска организатором":
                    self.set_status("Ожидает допуска организатором")
                await asyncio.sleep(0.1 if connected else 0.5)
        except Exception as exc:
            log.warning("Ошибка голосовой сессии Телемоста (%s)", type(exc).__name__)
            result = "Ошибка браузера, виртуального звука или голосового движка"
        finally:
            try:
                await voice.stop()
            except Exception as exc:
                log.warning("Ошибка закрытия голосового клиента (%s)", type(exc).__name__)
            finally:
                try:
                    await browser.close()
                except Exception as exc:
                    log.warning("Ошибка закрытия браузера (%s)", type(exc).__name__)
                self.set_status(result)

    @staticmethod
    async def forward_audio(browser, voice):
        for _ in range(12):
            try:
                pcm = browser.incoming.get_nowait()
            except asyncio.QueueEmpty:
                return
            if voice.ready:
                await voice.send_audio(SPEAKER, pcm)


async def main():
    client = WorkspaceClient(MANIFEST)
    workspace = Telemost(client)
    panel = asyncio.create_task(workspace.publish())
    try:
        return await client.run()
    finally:
        await workspace.disconnect()
        panel.cancel()
        await asyncio.gather(panel, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
