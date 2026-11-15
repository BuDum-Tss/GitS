"""Очередь реплик сессии и подача звука адаптеру в реальном времени.

Протокол речи тот же, что у модуля voice (docs/MODULES.md): реплика целиком одним куском, `replace` обрывает звучащее и
очищает очередь, без `replace` реплика встаёт в очередь (до 3 ожидающих, при переполнении вытесняется самая старая с причиной
`replaced`), `stop` обрывает сразу (`at: now`) или после звучащей реплики (`segment_end`). Конец реплики событием `ended`:
`completed` или причина обрыва, `spoken_upto` в миллисекундах сыгранного и `spoken_text` (начало текста по доле сыгранного).

Синтез начинается при постановке в очередь (следующая реплика готова к моменту, когда допоётся текущая). Кадры по 20 мс
уходят адаптеру в темпе реального времени с небольшим опережением `lead_ms` (адаптер держит свой короткий буфер);
фактически сыгранное сообщает адаптер подтверждением `play.ack`, без него берётся расчёт по часам.
"""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .frames import FRAME_MS, pcm_ms, split_frames
from .protocol import encode
from .tts import TtsClient, TtsError

logger = logging.getLogger("voice_engine.playback")

MAX_WAITING = 3


@dataclass
class PlaybackConfig:
    max_waiting: int = MAX_WAITING
    lead_ms: int = 60                  # на сколько кадры опережают воспроизведение
    ack_grace_s: float = 1.5           # сколько ждать подтверждения адаптера после последнего кадра
    stop_grace_s: float = 1.0          # и после play.stop
    voice: str = ""
    emotion: str = ""


@dataclass
class Utterance:
    uid: str
    text: str
    audio: asyncio.Task | None = None
    play_id: int = 0
    stop_reason: str | None = None
    ack: asyncio.Future | None = None          # (spoken_ms, reason) от адаптера
    started_at: float = 0.0
    total_ms: int = 0
    frames_sent: int = 0


@dataclass(frozen=True)
class PlaybackEvent:
    phase: str                          # started | ended
    uid: str
    text: str = ""
    completed: bool = False
    reason: str | None = None
    spoken_ms: int = 0
    total_ms: int = 0
    spoken_text: str = ""


class Playback:
    """Очередь реплик одной сессии. `transport` даёт `send_control(текст JSON)` и `send_play_frame(play_id, seq, pcm)`."""

    def __init__(self, transport, tts: TtsClient, clock, on_event: Callable[[PlaybackEvent], None],
                 cfg: PlaybackConfig | None = None, play_ids: Callable[[], int] | None = None):
        self._tr, self._tts, self._clock, self._emit = transport, tts, clock, on_event
        self.cfg = cfg or PlaybackConfig()
        self._waiting: deque[Utterance] = deque()
        self._current: Utterance | None = None
        self._play_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._audible = False
        self._closed = False
        self._counter = 0
        self._play_ids = play_ids or self._own_play_id
        self.last_end_ts = 0.0

    def _own_play_id(self) -> int:
        self._counter += 1
        return self._counter

    # ── состояние ────────────────────────────────────────────────────────────────

    @property
    def audible(self) -> bool:
        """Адаптер сейчас воспроизводит нашу речь (от первого кадра до конца): по этому признаку работает перебивание."""
        return self._audible

    @property
    def current_uid(self) -> str | None:
        return self._current.uid if self._current is not None else None

    def waiting_uids(self) -> list[str]:
        return [u.uid for u in self._waiting]

    def busy(self) -> bool:
        return self._current is not None or bool(self._waiting)

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run(), name="voice-playback")

    async def aclose(self) -> None:
        """Закрыть сессию: всё звучащее и ожидающее заканчивается с причиной `closed`; адаптеру команда остановки не шлётся."""
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        if self._play_task is not None:
            self._play_task.cancel()
            await asyncio.gather(self._play_task, return_exceptions=True)
        cur, self._current, self._audible = self._current, None, False
        if cur is not None:
            self._finish(cur, completed=False, reason="closed", spoken_ms=self._estimate(cur))
        self._drop_waiting("closed")

    # ── вход ─────────────────────────────────────────────────────────────────────

    def speak(self, uid: str, text: str, replace: bool = False) -> int:
        """Поставить реплику. Возвращает её место в очереди: 0 звучит первой (или заменила звучащую), далее по порядку."""
        if self._closed:
            raise RuntimeError("сессия закрыта")
        if replace:
            self._drop_waiting("replaced")
            self._interrupt("replaced")
        else:
            while len(self._waiting) >= self.cfg.max_waiting:
                self._discard(self._waiting.popleft(), "replaced")
        utt = Utterance(uid=uid, text=text)
        utt.audio = asyncio.get_running_loop().create_task(self._synth(text), name=f"voice-tts-{uid}")
        utt.audio.add_done_callback(_consume)
        self._waiting.append(utt)
        self._wake.set()
        self.start()
        if replace:
            return 0
        return len(self._waiting) - 1 + (1 if self._current is not None else 0)

    def stop(self, uid: str | None = None, at: str = "now", reason: str = "stop") -> None:
        """Остановить: uid None это всё (звучащее и очередь). at `now` обрывает звучащее сразу, `segment_end` дает ему договорить
        (реплика одним куском, поэтому это «дослушать текущую»); очередь очищается в обоих случаях."""
        if uid is None:
            self._drop_waiting(reason)
            if self._current is not None and at == "now":
                self._interrupt(reason)
            return
        for utt in list(self._waiting):
            if utt.uid == uid:
                self._waiting.remove(utt)
                self._discard(utt, reason)
                return
        if self._current is not None and self._current.uid == uid and at == "now":
            self._interrupt(reason)

    def barge_in(self) -> bool:
        """Перебивание: оборвать звучащее и очистить очередь. False, если ничего не звучало."""
        cur = self._current
        if not self._audible or cur is None or cur.stop_reason is not None:
            return False
        self.stop(None, "now", "barge_in")
        return True

    def on_ack(self, uid: str, spoken_ms: int, reason: str) -> None:
        """Подтверждение адаптера `play.ack`."""
        cur = self._current
        if cur is not None and cur.uid == uid and cur.ack is not None and not cur.ack.done():
            cur.ack.set_result((spoken_ms, reason))

    # ── внутреннее ───────────────────────────────────────────────────────────────

    async def _synth(self, text: str) -> bytes:
        return await self._tts.synthesize(text, voice=self.cfg.voice or None, emotion=self.cfg.emotion or None)

    def _discard(self, utt: Utterance, reason: str) -> None:
        if utt.audio is not None:
            utt.audio.cancel()
        self._finish(utt, completed=False, reason=reason, spoken_ms=0)

    def _drop_waiting(self, reason: str) -> None:
        while self._waiting:
            self._discard(self._waiting.popleft(), reason)

    def _interrupt(self, reason: str) -> None:
        cur = self._current
        if cur is not None and cur.stop_reason is None:
            cur.stop_reason = reason
            if self._play_task is not None:
                self._play_task.cancel()

    def _finish(self, utt: Utterance, *, completed: bool, reason: str | None, spoken_ms: int) -> None:
        total = utt.total_ms
        spoken_text = utt.text if completed else (_cut_text(utt.text, spoken_ms / total) if total else "")
        self.last_end_ts = self._clock.time()
        try:
            self._emit(PlaybackEvent("ended", utt.uid, utt.text, completed, None if completed else (reason or "stop"),
                                     spoken_ms, total, spoken_text))
        except Exception:
            logger.exception("Обработчик события речи упал")

    def _estimate(self, utt: Utterance) -> int:
        """Сколько мс успело прозвучать по часам (адаптер не подтвердил)."""
        if not utt.started_at:
            return 0
        elapsed_ms = int((self._clock.monotonic() - utt.started_at) * 1000)
        return max(0, min(utt.total_ms, elapsed_ms))

    async def _run(self) -> None:
        while True:
            while not self._waiting:
                self._wake.clear()
                await self._wake.wait()
            utt = self._waiting.popleft()
            self._current = utt
            self._play_task = asyncio.get_running_loop().create_task(self._play(utt), name=f"voice-play-{utt.uid}")
            try:
                await asyncio.wait({self._play_task})
            except asyncio.CancelledError:
                self._play_task.cancel()
                raise
            task, self._play_task = self._play_task, None
            await self._settle(utt, task)
            self._current, self._audible = None, False

    async def _settle(self, utt: Utterance, task: asyncio.Task) -> None:
        """Итог проигрывания: нормальный конец, обрыв (остановка адаптеру и ожидание его подтверждения) или ошибка."""
        if task.cancelled():
            reason = utt.stop_reason or "stop"
            spoken = await self._stop_adapter(utt, reason)
            self._finish(utt, completed=False, reason=reason, spoken_ms=spoken)
            return
        exc = task.exception()
        if exc is None:
            spoken, reason = task.result()
            self._finish(utt, completed=reason == "completed", reason=reason, spoken_ms=spoken)
            return
        if isinstance(exc, TtsError):
            logger.warning("Синтез не удался (%s)", type(exc).__name__)
        elif not isinstance(exc, (ConnectionError, RuntimeError)):
            logger.error("Воспроизведение реплики упало: %r", exc)
        self._finish(utt, completed=False, reason="error", spoken_ms=self._estimate(utt))

    async def _stop_adapter(self, utt: Utterance, reason: str) -> int:
        """Сказать адаптеру остановиться и узнать, сколько он сыграл; без ответа в срок берётся расчёт по часам."""
        if not utt.started_at:
            return 0
        fallback = self._estimate(utt)
        try:
            await self._tr.send_control(encode("play.stop", utterance_id=utt.uid, reason=reason))
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            return fallback
        got = await self._wait_ack(utt, self.cfg.stop_grace_s)
        return min(utt.total_ms, got[0]) if got else fallback

    async def _wait_ack(self, utt: Utterance, timeout: float) -> tuple[int, str] | None:
        if utt.ack is None:
            return None
        if utt.ack.done():
            return utt.ack.result()
        sleeper = asyncio.ensure_future(self._clock.sleep(timeout))
        try:
            await asyncio.wait({utt.ack, sleeper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            sleeper.cancel()
        return utt.ack.result() if utt.ack.done() else None

    async def _play(self, utt: Utterance) -> tuple[int, str]:
        """Синтез (уже идёт), затем кадры по 20 мс. Возвращает (сыграно мс, причина)."""
        pcm = await utt.audio
        frames = split_frames(pcm)
        if not frames:
            raise TtsError("синтез вернул тишину")
        utt.total_ms = pcm_ms(pcm)
        utt.play_id = self._play_ids()
        utt.ack = asyncio.get_running_loop().create_future()
        await self._tr.send_control(encode("play.start", utterance_id=utt.uid, play_id=utt.play_id, text=utt.text[:4000],
                                           total_ms=utt.total_ms))
        utt.started_at = self._clock.monotonic()
        self._audible = True
        self._emit(PlaybackEvent("started", utt.uid, utt.text, total_ms=utt.total_ms))
        step, lead = FRAME_MS / 1000.0, self.cfg.lead_ms / 1000.0
        for i, frame in enumerate(frames):
            delay = utt.started_at + i * step - lead - self._clock.monotonic()
            if delay > 0:
                await self._clock.sleep(delay)
            await self._tr.send_play_frame(utt.play_id, i, frame)
            utt.frames_sent = i + 1
        await self._tr.send_control(encode("play.end", utterance_id=utt.uid, play_id=utt.play_id))
        remaining = utt.started_at + len(frames) * step - self._clock.monotonic()
        got = await self._wait_ack(utt, max(0.0, remaining) + self.cfg.ack_grace_s)
        if got is None:
            return utt.total_ms, "completed"
        spoken, reason = got
        return min(utt.total_ms, spoken), reason


def _cut_text(text: str, ratio: float) -> str:
    """Начало текста по доле сыгранного, обрезанное по границе слова."""
    if ratio <= 0:
        return ""
    cut = text[:int(len(text) * min(ratio, 1.0))]
    if cut != text and " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.strip()


def _consume(task: asyncio.Task) -> None:
    """Исключение отброшенного синтеза не должно попасть в «Task exception was never retrieved»."""
    if not task.cancelled():
        task.exception()

