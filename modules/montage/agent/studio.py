"""Remotion Studio для задачи монтажа (ТЗ 15, TAB-4): `npx remotion studio` в рабочей копии задачи `jobs/<id>/work` с
`--props=../plan/plan.json` на свободном порту 127.0.0.1. Один Studio на задачу: повторный вызов отдаёт тот же адрес.
Студия гаснет вместе с задачей (`stop`) или после IDLE_STOP_S без обращений (`reap`). Рендера здесь нет: Studio только
показывает ролик и план. Объявление запуска делает менеджер (событие `montage.launch`), не этот модуль.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

import media

log = logging.getLogger("montage.studio")

IDLE_STOP_S = 30 * 60           # без обращений Studio гаснет (ТЗ 15, TAB-4)
START_TIMEOUT_S = 100           # Studio должен ответить за это время (инструмент ждёт не больше 120 с, манифест)
REAP_EVERY_S = 60
PROPS_REL = "../plan/plan.json"  # относительно рабочей копии задачи
STUDIO_ENTRY = "src/index.ts"


class StudioError(RuntimeError):
    """Studio не поднялся (текст для агента и журнала)."""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:           # noqa: S310 - только 127.0.0.1
            return resp.status < 500
    except OSError:
        return False


async def _spawn_studio(npx: str, work: Path, port: int):
    args = [npx, "remotion", "studio", STUDIO_ENTRY, f"--port={port}", f"--props={PROPS_REL}", "--no-open"]
    return await asyncio.create_subprocess_exec(
        *args, cwd=str(work), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, creationflags=media._NO_WINDOW | media._NEW_GROUP)


async def _wait_ready(url: str, proc) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            raise StudioError(f"Remotion Studio завершился при запуске (код {proc.returncode})")
        if await asyncio.to_thread(_answers, url):
            return
        await asyncio.sleep(1)
    raise StudioError(f"Remotion Studio не ответил за {START_TIMEOUT_S} с")


async def _kill(proc) -> None:
    if proc.returncode is None:
        await asyncio.to_thread(media.kill_tree, proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            log.warning("Studio (pid %s) не завершился после kill_tree", proc.pid)


class _Run:
    def __init__(self, proc, url: str, last: float):
        self.proc, self.url, self.last = proc, url, last

    def alive(self) -> bool:
        return self.proc.returncode is None


class StudioPool:
    """Studio по задачам. Подмены для тестов: npx, clock, spawn, ready, port."""

    def __init__(self, *, npx: Callable[[], str] = media.find_npx, clock: Callable[[], float] = time.monotonic,
                 idle_s: float = IDLE_STOP_S, spawn=None, ready=None, port: Callable[[], int] = free_port):
        self._npx = npx
        self._clock = clock
        self.idle_s = idle_s
        self._spawn = spawn or _spawn_studio
        self._ready = ready or _wait_ready
        self._port = port
        self._runs: dict[str, _Run] = {}
        self._lock = asyncio.Lock()

    def running(self) -> list[str]:
        return [job_id for job_id, run in self._runs.items() if run.alive()]

    async def open(self, job_id: str, work: Path) -> tuple[str, bool]:
        """Адрес Studio задачи и признак «поднят только что». Один Studio на задачу."""
        async with self._lock:
            run = self._runs.get(job_id)
            if run is not None and run.alive():
                run.last = self._clock()
                return run.url, False
            if not Path(work).is_dir():
                raise StudioError("рабочей копии задачи нет")
            port = self._port()
            url = f"http://127.0.0.1:{port}"
            proc = await self._spawn(self._npx(), Path(work), port)
            run = _Run(proc, url, self._clock())
            self._runs[job_id] = run
            try:
                await self._ready(url, proc)
            except Exception:
                self._runs.pop(job_id, None)
                await _kill(proc)
                raise
            return url, True

    async def stop(self, job_id: str) -> bool:
        run = self._runs.pop(job_id, None)
        if run is None:
            return False
        await _kill(run.proc)
        return True

    async def stop_all(self) -> None:
        for job_id in list(self._runs):
            await self.stop(job_id)

    async def reap(self) -> list[str]:
        """Погасить Studio без обращений дольше idle_s и уже упавшие. Возвращает id погашенных."""
        now = self._clock()
        gone = []
        for job_id, run in list(self._runs.items()):
            if not run.alive() or now - run.last > self.idle_s:
                await self.stop(job_id)
                gone.append(job_id)
        return gone

    async def run_reaper(self) -> None:
        while True:
            await asyncio.sleep(REAP_EVERY_S)
            try:
                await self.reap()
            except Exception as e:
                log.warning("проверка простоя Studio: %s", e)

