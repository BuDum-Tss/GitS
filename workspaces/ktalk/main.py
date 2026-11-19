import asyncio
import copy
import logging
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.extend([str(ROOT), str(ROOT / "agent" / "interfaces" / "workspace")])

from gits_sdk import ToolError  # noqa: E402

from workspaces.ktalk.browser import KTalkBrowser  # noqa: E402
from workspaces.telemost.browser import FRAME_BYTES  # noqa: E402
from workspaces.telemost.main import MANIFEST as BROWSER_MANIFEST  # noqa: E402
from workspaces.telemost.main import Telemost, main  # noqa: E402

TITLE = "Контур.Толк"
LINK = re.compile(r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ktalk\.ru/[a-z0-9_-]{1,128}/?(?:\?[^\s#]*)?", re.I)
MANIFEST = copy.deepcopy(BROWSER_MANIFEST)
MANIFEST.update(id="ktalk", title=TITLE, description="Созвоны Контур.Толка через браузер с отдельными WebRTC-аудиодорожками.")
MANIFEST["panel"].update(title=TITLE, order=63)
for tool in MANIFEST["tools"]:
    tool["description"] = tool["description"].replace("Яндекс Телемоста", TITLE)
    tool["keywords"] = ["ktalk", "контур", "толк", *tool["keywords"][2:]]
MANIFEST["tools"][0]["params"]["link"]["description"] = "Ссылка https://пространство.ktalk.ru/комната"


def meeting_url(link):
    if not isinstance(link, str) or not LINK.fullmatch(link.strip()):
        raise ToolError("Нужна HTTPS-ссылка на комнату *.ktalk.ru", code="bad_request")
    return link.strip()


class KTalk(Telemost):
    adapter = "ktalk"
    title = TITLE
    placeholder = "https://пространство.ktalk.ru/комната"
    parse_link = staticmethod(meeting_url)
    speakers_labelled = True

    browser_class = KTalkBrowser

    async def register_speakers(self, voice):
        self.buffers = {}
        self.speaker_count = 0
        self.labels = {}

    async def forward_audio(self, browser, voice):
        names = getattr(browser, "names", {})
        for speaker, label in list(self.labels.items()):          # имя нашлось позже, чем началась речь: обновить подпись
            if names.get(speaker) and names[speaker] != label and speaker in self.buffers:
                self.labels[speaker] = names[speaker]
                await voice.speaker_join(speaker, names[speaker])
        for _ in range(64):
            try:
                speaker, pcm = browser.incoming.get_nowait()
            except asyncio.QueueEmpty:
                return
            if pcm is None:
                self.labels.pop(speaker, None)
                if self.buffers.pop(speaker, None) is not None:
                    await voice.speaker_leave(speaker)
                continue
            if not voice.ready:
                continue
            if speaker not in self.buffers:
                self.speaker_count += 1
                self.labels[speaker] = names.get(speaker) or f"Участник {self.speaker_count}"
                await voice.speaker_join(speaker, self.labels[speaker])
                self.buffers[speaker] = bytearray()
            buf = self.buffers[speaker]
            buf.extend(pcm)
            while len(buf) >= FRAME_BYTES:
                await voice.send_audio(speaker, bytes(buf[:FRAME_BYTES]))
                del buf[:FRAME_BYTES]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main(MANIFEST, KTalk)))
