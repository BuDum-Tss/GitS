"""Запись забега в контейнерном режиме: то же, что `RunRecorder` для контроллера, но захват (ffmpeg, звук) идёт на хосте.

Метки, исход, цели и события забега остаются у модуля; хостовому помощнику они отдаются при закрытии записи
(`HostBridge.record_finish`), и сценарий, описание для монтажа и сами файлы лежат на хосте рядом с записью. Здесь хранится
зеркало состояния (`current`) с заглушками процесса и звука: наблюдатель за игрой читает у них те же поля, что у настоящих
(`proc.poll()`, `audio.ok`, `audio.stopping`). Зеркало обновляет `refresh()`, его зовёт наблюдатель перед проверкой.
"""
import asyncio
import logging
import time
from pathlib import Path

import events_log
from run_recorder import Recording, RunRecorder

logger = logging.getLogger("remote_recorder")


class _RemoteProc:
    """Вместо `subprocess.Popen`: `poll()` None, пока ffmpeg на хосте жив."""

    def __init__(self):
        self.code: int | None = None

    def poll(self):
        return self.code


class _RemoteAudio:
    """Вместо `LoopbackRecorder`: только то, что читают наблюдатель и монтаж модуля."""

    def __init__(self):
        self.ok = True
        self.stopping = False
        self.error = ""
        self.parts: list = []
        self.sound = False

    def has_sound(self) -> bool:
        return self.sound


def _apply(rec: Recording, status: dict | None) -> None:
    """Перенести статус хоста на зеркало; None (хост записи не знает) считается мёртвым захватом."""
    if status is None:
        rec.proc.code = 1
        return
    rec.remote_id = status.get("id")
    rec.proc.code = None if status.get("video_alive") else 1
    if status.get("audio_present"):
        if rec.audio is None:
            rec.audio = _RemoteAudio()
        rec.audio.ok = bool(status.get("audio_ok"))
        rec.audio.stopping = bool(status.get("audio_stopping"))
        rec.audio.error = str(status.get("audio_error") or "")
        rec.audio.sound = bool(status.get("has_sound"))
    else:
        rec.audio = None


def _mirror(status: dict, run_id: str, colony: str) -> Recording:
    rec = Recording(run_id, colony, Path(str(status.get("raw") or "")), float(status.get("started") or time.time()),
                    proc=_RemoteProc(), audio=_RemoteAudio())
    _apply(rec, status)
    return rec


class RemoteRunRecorder(RunRecorder):
    def __init__(self, host, memory_dir, settings_get, max_hours=None):
        super().__init__(memory_dir, settings_get, max_hours=max_hours)
        self._host = host

    # ── начало ──
    def start(self, run_id: str, colony: str, why: str = "забег начался"):
        """Начать запись. Зеркало появляется сразу, хост отвечает позже (метод вызывается из цикла событий и не ждёт)."""
        if not self._settings().get("record_runs") or self.current is not None:
            return
        rec = Recording(run_id, colony, Path(""), time.time(), proc=_RemoteProc(), audio=_RemoteAudio())
        rec.remote_id = None
        self.current = rec
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            rec.start_task = loop.create_task(self._start_remote(rec, why))
        else:
            asyncio.run(self._start_remote(rec, why))

    async def _start_remote(self, rec: Recording, why: str):
        try:
            status = await self._host.record_start(rec.run_id, rec.colony, why)
        except Exception as e:
            logger.warning(f"Запись забега не началась на хосте: {e}")
            status = None
        if status is None:
            if self.current is rec:
                self.current = None
            return
        rec.raw = Path(str(status.get("raw") or ""))
        _apply(rec, status)

    # ── наблюдение ──
    def refresh(self) -> None:
        """Обновить зеркало по хосту (блокирующая: из рабочего потока наблюдателя). Нет связи: исключение, зеркало не трогаем."""
        rec = self.current
        if rec is None or getattr(rec, "remote_id", None) is None:
            return
        _apply(rec, self._host.blocking(self._host.record_status()))

    def ensure_alive(self) -> Recording | None:
        rec = self.current
        if rec is None or getattr(rec, "remote_id", None) is None:
            return None
        res = self._host.blocking(self._host.record_ensure_alive(max_hours=self._max_hours()))
        status, closed = res.get("status"), res.get("closed_id")
        if closed is None and status is not None:
            _apply(rec, status)
            return None
        old = self.close("part")
        old.remote_id = closed                              # None: хост запись потерял (перезапуск), монтировать нечего
        if closed is None:
            events_log.log("video", "хостовый помощник потерял запись забега: начинаю новую")
            status = self._host.blocking(self._host.record_start(old.run_id, old.colony, "запись потеряна помощником"))
        if status is not None:
            self.current = _mirror(status, old.run_id, old.colony)
        return old

    def ensure_audio(self) -> bool:
        rec = self.current
        if rec is None or rec.audio is None or rec.audio.ok or rec.audio.stopping:
            return False
        started = self._host.blocking(self._host.record_ensure_audio())
        self.refresh()
        return started

    # ── конец ──
    def _markers(self, rec: Recording) -> list:
        return [list(m) for m in rec.markers]

    async def _finish(self, rec: Recording, mode: str) -> dict | None:
        task = getattr(rec, "start_task", None)
        if task is not None:
            await asyncio.wait({task})
        rid = getattr(rec, "remote_id", None)
        if rid is None:
            logger.warning("Запись забега не была начата на хосте: монтировать нечего")
            return None
        events = None
        if mode != "stop":
            events = await asyncio.to_thread(events_log.tail, limit=20_000, kinds={"game", "say"}, since=rec.started)
        try:
            return await self._host.record_finish(rid, rec.outcome, mode, self._markers(rec), rec.goal, events)
        except Exception as e:
            logger.error(f"Хост не завершил запись забега ({mode}): {e}")
            return None

    async def produce(self, rec: Recording) -> dict | None:
        return await self._finish(rec, "montage")

    async def produce_for_montage(self, rec: Recording) -> dict | None:
        return await self._finish(rec, "handoff")

    def process(self, rec: Recording) -> dict | None:
        """Собственный монтаж уже остановленной записи (блокирующая: из потока)."""
        rid = getattr(rec, "remote_id", None)
        if rid is None:
            return None
        events = events_log.tail(limit=20_000, kinds={"game", "say"}, since=rec.started)
        try:
            return self._host.blocking(self._host.record_finish(rid, rec.outcome, "montage", self._markers(rec), rec.goal, events))
        except Exception as e:
            logger.error(f"Хост не смонтировал запись забега: {e}")
            return None

    def stop_blocking(self, rec: Recording):
        rid = getattr(rec, "remote_id", None)
        if rid is None:
            return
        self._host.blocking(self._host.record_finish(rid, rec.outcome, "stop", self._markers(rec), rec.goal, None))
