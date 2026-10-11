"""Очередь реплик и воспроизведение (ТЗ 12, раздел 5: протокол речи, VOICE-3, VOICE-4).

Ядро шлёт `speak` (реплика целиком) и `speak.stop`; модуль режет реплику на предложения (`segmenter.split_sentences`),
синтезирует и играет их по одному, а событиями `voice.speech` сообщает, что звучит: `started` (первое предложение),
`segment` (следующие), `ended` (`completed` или причина обрыва `stop` / `replaced` / `barge_in` / `muted` / `error`).

Предзагрузка (VOICE-3): пока звучит предложение, следующее уже синтезируется, паузы между предложениями не растут. Реплика
с `replace: true` обрывает звучащее сразу и очищает очередь ожидающих; `replace: false` ставит реплику в очередь (до 3
ожидающих, при переполнении выбрасывается самая старая из ожидающих с `ended reason replaced`). `speak.stop` с `at: now`
обрывает сразу (процесс воспроизведения убивается), `at: segment_end` дослушивает текущее предложение и остановится; если оно
последнее, реплика считается договорённой (`completed: true`). `voice_muted` (VOICE-4): звук не играется, но `started` и
`ended muted` приходят сразу, чтобы ядро не ждало.

`spoken_upto` в `ended` это число предложений, проигранных до конца; оборванное посередине не считается.
"""
import asyncio
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from segmenter import split_sentences

logger = logging.getLogger("speaker")

MAX_WAITING = 3                  # сколько реплик может ждать своей очереди (не считая звучащей)


@dataclass
class Utterance:
    uid: str
    text: str
    segments: list[str]
    emotion: str | None = None
    spoken: int = 0                              # предложений проиграно до конца
    started: bool = False                        # первое предложение уже зазвучало
    cancel_reason: str | None = None             # оборвать сейчас, причина для ended
    stop_after_segment: str | None = None        # дослушать текущее предложение и остановиться, причина для ended
    tasks: set = field(default_factory=set)      # дочерние задачи (синтез, воспроизведение)


class Speaker:
    def __init__(self, *, synth: Callable[[str], Awaitable[str | None]], player,
                 emit: Callable[[dict], None], volume: Callable[[], float] = lambda: 1.0,
                 muted: Callable[[], bool] = lambda: False, on_segment: Callable[[str], None] | None = None):
        """synth(текст) -> путь к аудиофайлу или None; player.play(путь, громкость) играет и ждёт конца (отмена обрывает);
        emit(data) - событие voice.speech (синхронно, порядок сохраняется выше по течению); volume() - 0..1; muted() -
        `voice_muted`; on_segment(текст) - сегмент зазвучал (слух не слушает собственный голос)."""
        self._synth, self._player, self._emit = synth, player, emit
        self._volume, self._muted, self._on_segment = volume, muted, on_segment
        self._waiting: deque[Utterance] = deque()
        self._current: Utterance | None = None
        self._audible = False
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    # ── состояние ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        """Остановить воркер и оборвать звучащее (без событий: связь с ядром закрывается следом)."""
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        self._audible = False

    def is_speaking(self) -> bool:
        """Есть звучащая или ожидающая реплика."""
        return self._current is not None or bool(self._waiting)

    @property
    def audible(self) -> bool:
        """Звук сейчас идёт в колонки (с начала воспроизведения предложения до его конца): «ворота» микрофона (VOICE-7)."""
        return self._audible

    @property
    def current_uid(self) -> str | None:
        return self._current.uid if self._current is not None else None

    def waiting_uids(self) -> list[str]:
        return [u.uid for u in self._waiting]

    # ── вход ─────────────────────────────────────────────────────────────────

    async def speak(self, uid: str, text: str, emotion: str | None = None, replace: bool = False) -> None:
        segments = split_sentences(text or "")
        if not segments:
            self._ended(Utterance(uid, text or "", []), completed=False, reason="error")
            return
        u = Utterance(uid, text, segments, emotion)
        if self._muted():
            # VOICE-4: звука нет, но ядро не ждёт; облачко питомца видит весь текст
            self._emit({"phase": "started", "utterance_id": uid, "seq": 0, "segment": text.strip()})
            self._ended(u, completed=False, reason="muted")
            return
        if replace:
            self._drop_waiting("replaced")
            if self._current is not None:
                self._cancel_current(self._current, "replaced")
        else:
            while len(self._waiting) >= MAX_WAITING:
                self._ended(self._waiting.popleft(), completed=False, reason="replaced")      # переполнение: самая старая
        self._waiting.append(u)
        self._wake.set()

    async def stop(self, uid: str | None = None, at: str = "segment_end", reason: str = "stop") -> None:
        """`speak.stop`: uid None - всё и очередь; at: now | segment_end. Чужой uid (уже закончилась) игнорируется."""
        now = at == "now"
        if uid is None:
            self._drop_waiting(reason)
            if self._current is not None:
                self._interrupt(self._current, reason, now)
            return
        for u in list(self._waiting):
            if u.uid == uid:
                self._waiting.remove(u)
                self._ended(u, completed=False, reason=reason)
                return
        if self._current is not None and self._current.uid == uid:
            self._interrupt(self._current, reason, now)

    def mute_now(self) -> None:
        """Включили `voice_muted` посреди речи: звучащее и ожидающее обрываются (прежний `set_muted(True)`)."""
        self._drop_waiting("muted")
        if self._current is not None:
            self._cancel_current(self._current, "muted")

    # ── внутреннее ───────────────────────────────────────────────────────────

    def _ended(self, u: Utterance, *, completed: bool, reason: str | None = None) -> None:
        data = {"phase": "ended", "utterance_id": u.uid, "completed": completed, "spoken_upto": u.spoken}
        if not completed:
            data["reason"] = reason or "stop"
        self._emit(data)

    def _drop_waiting(self, reason: str) -> None:
        while self._waiting:
            self._ended(self._waiting.popleft(), completed=False, reason=reason)

    def _interrupt(self, u: Utterance, reason: str, now: bool) -> None:
        if now or not self._audible:
            self._cancel_current(u, reason)               # слышимого предложения нет: договаривать нечего
        elif u.cancel_reason is None:
            u.stop_after_segment = reason
            for t in [t for t in u.tasks if getattr(t, "_is_preload", False)]:
                t.cancel()                                # следующее предложение уже не понадобится

    def _cancel_current(self, u: Utterance, reason: str) -> None:
        if u.cancel_reason is None:
            u.cancel_reason = reason
        for t in list(u.tasks):
            t.cancel()

    @staticmethod
    def _unlink(path: str | None) -> None:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _spawn(self, u: Utterance, coro, *, preload: bool = False) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        task._is_preload = preload                        # type: ignore[attr-defined]
        u.tasks.add(task)
        task.add_done_callback(u.tasks.discard)
        return task

    @staticmethod
    async def _await(task: asyncio.Task):
        """Дождаться дочерней задачи: (результат, "ok" | "cancelled" | "error"). Отмену дочерней (обрыв) отличаем от отмены
        самого воркера: вторая пробрасывается."""
        try:
            await asyncio.wait([task])
        except asyncio.CancelledError:
            task.cancel()
            raise
        if task.cancelled():
            return None, "cancelled"
        exc = task.exception()
        if exc is not None:
            logger.warning(f"Сбой при озвучке: {type(exc).__name__}: {exc}")
            return None, "error"
        return task.result(), "ok"

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._waiting:
                u = self._waiting.popleft()
                self._current = u
                try:
                    await self._play(u)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception("Сбой воркера озвучки")
                    self._ended(u, completed=False, reason="error")
                    logger.warning(f"реплика {u.uid}: {type(e).__name__}")
                finally:
                    self._audible = False
                    self._current = None

    async def _play(self, u: Utterance) -> None:
        n = len(u.segments)
        failed = 0
        synths: dict[int, asyncio.Task] = {}                  # синтез предложений: номер -> задача (следующее готовится заранее)

        def ensure(j: int, preload: bool = False) -> None:
            if j < n and j not in synths:
                synths[j] = self._spawn(u, self._synth(u.segments[j]), preload=preload)

        try:
            ensure(0)
            for i in range(n):
                if u.cancel_reason:
                    break
                ensure(i)
                path, status = await self._await(synths.pop(i))
                if u.cancel_reason:
                    self._unlink(path)
                    break
                if u.stop_after_segment is None:
                    ensure(i + 1, preload=True)               # VOICE-3: пока звучит это предложение, следующее синтезируется
                if status != "ok" or not path:
                    failed += 1
                    continue
                seg = u.segments[i]
                self._audible = True
                self._emit({"phase": "segment" if u.started else "started", "utterance_id": u.uid, "seq": i, "segment": seg})
                u.started = True
                if self._on_segment is not None:
                    try:
                        self._on_segment(seg)
                    except Exception as e:
                        logger.debug(f"Слух не уведомлён о сегменте: {e}")
                _res, status = await self._await(self._spawn(u, self._player.play(path, self._volume())))
                self._audible = False
                self._unlink(path)
                if u.cancel_reason:
                    break
                if status != "ok":
                    failed += 1
                    continue
                u.spoken += 1
                if u.stop_after_segment is not None and i + 1 < n:
                    u.cancel_reason = u.stop_after_segment        # предложение дослушано: дальше не идём
                    break
        finally:
            for t in list(u.tasks):
                t.cancel()
            for t in synths.values():                             # недослушанная предзагрузка: файл не нужен
                if t.done() and not t.cancelled() and t.exception() is None:
                    self._unlink(t.result())
        completed = u.cancel_reason is None and u.spoken == n and failed == 0
        reason = u.cancel_reason or ("error" if failed else "stop")
        self._ended(u, completed=completed, reason=reason)
