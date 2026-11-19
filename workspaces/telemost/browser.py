import asyncio
import os
import re
import shutil
from contextlib import AsyncExitStack, suppress
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory

FRAME_BYTES = 640
PULSE_TOOLS = ("pulseaudio", "pactl", "parec", "pacat")
JOIN_TIMEOUT = 110
JOIN = re.compile(r"^(?:Подключиться(?: к (?:встрече|звонку)| со звуком)?|Присоединиться(?: к (?:встрече|звонку))?|Войти во встречу|Join(?: (?:meeting|call))?)$", re.I)
WEB = re.compile(r"(?:продолжить|открыть|подключиться|присоединиться).*(?:браузер|browser)|continue in (?:the )?browser", re.I)
LEAVE = re.compile(r"^(?:Выйти(?: (?:из (?:встречи|звонка|конференции)|со встречи))?|Покинуть (?:встречу|звонок)|Завершить (?:встречу|звонок)|Leave(?: (?:meeting|call))?|Hang up)$", re.I)
WAITING = re.compile(r"комнат[аеу] ожидания|допустит|запрос.*отправлен|waiting room|waiting to join", re.I)
NAME = re.compile(r"^имя$|ваше имя|введите имя|имя участника|your name", re.I)
AUTH = re.compile(r"^(?:Войдите в аккаунт|Sign in to your account)$", re.I)
MIC_OFF = re.compile(r"^(?:Включить микрофон|Unmute(?: microphone)?)$", re.I)
CAMERA_ON = re.compile(r"^(?:Выключить камеру|Turn off camera)$", re.I)


def browser_available():
    return find_spec("playwright") is not None and all(map(shutil.which, PULSE_TOOLS))


async def stop_process(process):
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class BrowserCall:
    def __init__(self):
        self.resources = AsyncExitStack()
        self.incoming = asyncio.Queue(maxsize=50)
        self.page = self.capture = self.player = None
        self.join_clicked = self.connected = False
        self.auth_since = None

    async def spawn(self, *args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.DEVNULL, **kwargs)
        self.resources.push_async_callback(stop_process, process)
        return process

    async def open(self, number):
        from playwright.async_api import async_playwright

        temp = self.resources.enter_context(TemporaryDirectory(prefix="gits-tm-"))
        socket = Path(temp) / "pulse.sock"
        self.audio_env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR") if key in os.environ}
        self.audio_env.update(PULSE_SERVER=f"unix:{socket}", XDG_RUNTIME_DIR=temp)
        pulse = await self.spawn(
            "pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--log-target=stderr",
            f"--load=module-native-protocol-unix socket={socket} auth-anonymous=1",
            "--load=module-null-sink sink_name=meeting rate=48000",
            "--load=module-null-sink sink_name=agent rate=48000",
            "--load=module-remap-source master=agent.monitor source_name=agent_mic",
            env=dict(self.audio_env, PULSE_RUNTIME_PATH=temp),
        )
        for _ in range(50):
            if pulse.returncode is not None:
                raise RuntimeError("PulseAudio завершился при запуске")
            if socket.exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("PulseAudio не открыл сокет")

        # Привязка к отдельным виртуальным устройствам, даже если Chromium проигнорирует PULSE_SOURCE.
        for operation, device in (("set-default-sink", "meeting"), ("set-default-source", "agent_mic")):
            command = await asyncio.create_subprocess_exec(
                "pactl", operation, device, env=self.audio_env, stderr=asyncio.subprocess.DEVNULL,
            )
            if await command.wait():
                raise RuntimeError("Не удалось настроить виртуальный звук")

        playwright = await self.resources.enter_async_context(async_playwright())
        executable = os.environ.get("GITS_TELEMOST_CHROMIUM") or shutil.which("chromium")
        browser = await playwright.chromium.launch(
            channel="chromium" if not executable else None, executable_path=executable,
            ignore_default_args=["--mute-audio"], args=["--disable-dev-shm-usage"],
            env=dict(self.audio_env, PULSE_SINK="meeting", PULSE_SOURCE="agent_mic"),
            timeout=10000,
        )
        self.resources.push_async_callback(browser.close)
        context = await browser.new_context(locale="ru-RU")
        self.resources.push_async_callback(context.close)
        origin = "https://telemost.yandex.ru"
        await context.grant_permissions(["microphone"], origin=origin)
        self.page = await context.new_page()
        await self.page.goto(f"{origin}/j/{number}", wait_until="domcontentloaded", timeout=12000)

    async def poll(self):
        page = self.page
        if page.is_closed() or (self.capture is not None and self.capture.returncode is not None):
            return "ended"
        waiting = await page.get_by_text(WAITING).first.is_visible()
        in_call = await page.get_by_role("button", name=LEAVE).first.is_visible()
        if in_call and not waiting:
            if not self.connected:
                mic = page.get_by_role("button", name=MIC_OFF).first
                if await mic.is_visible() and await mic.is_enabled():
                    await mic.click(timeout=1500)
                await self.start_capture()
                self.connected = True
            return "connected"
        if self.connected:
            return "ended"
        if waiting:
            return "waiting"
        web = page.get_by_role("button", name=WEB).or_(page.get_by_role("link", name=WEB)).first
        if await web.is_visible():
            await web.click(timeout=1500)
            return "connecting"
        join = page.get_by_role("button", name=JOIN).first
        if not self.join_clicked and await join.is_visible():
            self.auth_since = None
            name = page.get_by_role("textbox", name=NAME).or_(page.get_by_placeholder(NAME)).first
            if not await name.is_visible():
                textboxes = page.get_by_role("textbox")
                if await textboxes.count() == 1:
                    name = textboxes.first
            if await name.is_visible() and await name.is_editable() and await name.input_value(timeout=1500) != "ГЛаДОС":
                await name.fill("ГЛаДОС", timeout=1500)
            camera = page.get_by_role("button", name=CAMERA_ON).first
            if await camera.is_visible() and await camera.is_enabled():
                await camera.click(timeout=1500)
            if await join.is_enabled():
                await join.click(timeout=1500)
                self.join_clicked = True
            return "connecting"
        if await page.get_by_text(AUTH).first.is_visible():
            now = asyncio.get_running_loop().time()
            if self.auth_since is None:
                self.auth_since = now
            if now - self.auth_since >= 3:
                return "auth_required"
        else:
            self.auth_since = None
        return "connecting"

    async def start_capture(self):
        self.capture = await self.spawn(
            "parec", "--device=meeting.monitor", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
            "--latency-msec=40", env=self.audio_env, stdout=asyncio.subprocess.PIPE,
        )
        reader = asyncio.create_task(self.read_audio())
        self.resources.push_async_callback(self.stop_reader, reader)

    @staticmethod
    async def stop_reader(reader):
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def read_audio(self):
        try:
            while True:
                pcm = await self.capture.stdout.readexactly(FRAME_BYTES)
                if self.incoming.full():
                    self.incoming.get_nowait()
                self.incoming.put_nowait(pcm)
        except asyncio.IncompleteReadError:
            return

    async def play(self, frames, _uid):
        # Один pacat на реплику: остановка процесса сбрасывает очередь PulseAudio.
        process = await asyncio.create_subprocess_exec(
            "pacat", "--playback", "--device=agent", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
            "--latency-msec=40", env=self.audio_env, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        self.player = process
        frames_sent = 0
        started = asyncio.get_running_loop().time()
        try:
            async for pcm in frames:
                if frames.stop_reason is not None or process.returncode is not None:
                    break
                process.stdin.write(pcm)
                await process.stdin.drain()
                frames_sent += len(pcm) // 2
                await asyncio.sleep(max(0, started + frames_sent / 16000 - 0.04 - asyncio.get_running_loop().time()))
            if frames.stop_reason is None and process.returncode is None:
                process.stdin.close()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), 2)
        except ConnectionError:
            pass
        finally:
            played_ms = int(min(frames_sent / 16, (asyncio.get_running_loop().time() - started) * 1000))
            await stop_process(process)
            if self.player is process:
                self.player = None
        return played_ms

    def stop_playback(self):
        if self.player is not None and self.player.returncode is None:
            with suppress(ProcessLookupError):
                self.player.terminate()

    async def close(self):
        self.stop_playback()
        await self.resources.aclose()
