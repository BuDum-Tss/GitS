"""Очередь реплик сессии и подача звука адаптеру в реальном времени, с конвейерным синтезом по предложениям.

Протокол речи тот же, что у модуля voice (docs/MODULES.md): `replace` обрывает звучащее и очищает очередь, без `replace` реплика
встаёт в очередь (до 3 ожидающих, при переполнении вытесняется самая старая с причиной `replaced`), `stop` обрывает сразу
(`at: now`) или после звучащего предложения (`segment_end`: оно дослушивается, остальные отбрасываются; если оно было последним,
реплика считается договорённой). События: `started` (первое предложение зазвучало), `segment` (каждое следующее), `ended`
(`completed` или причина обрыва) со `spoken_segments` (целых проигранных предложений, это `spoken_upto` старого протокола),
`spoken_ms` и `spoken_text`.

Реплика делится на предложения (`sentences.py`). Первый звук идёт после синтеза только первого предложения; пока оно звучит,
синтезируются следующие (глубина предзагрузки `prefetch`, по одному запросу к сервису синтеза за раз на сессию). Кадры по 20 мс
идут адаптеру без зазоров, пока следующее предложение готово; если синтез не успел, адаптеру не льётся тишина: поток ждёт
готовности (это «разрыв», он считается в метриках). Ошибка синтеза одного предложения его пропускает, остальные звучат.
Фактически сыгранное сообщает адаптер подтверждением `play.ack`, без него берётся расчёт по часам.
"""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .frames import FRAME_MS, pcm_ms, split_frames
from .protocol import encode
from .sentences import MAX_CHARS, MIN_CHARS, split_sentences
from .tts import TtsClient, TtsError

logger = logging.getLogger("voice_engine.playback")

MAX_WAITING = 3
FRAME_TOLERANCE_MS = FRAME_MS          # предложение считается проигранным, если не хватает не больше одного кадра


@dataclass
class PlaybackConfig:
    max_waiting: int = MAX_WAITING
    lead_ms: int = 60                  # на сколько кадры опережают воспроизведение
    ack_grace_s: float = 1.5           # сколько ждать подтверждения адаптера после последнего кадра
    stop_grace_s: float = 1.0          # и после play.stop
    voice: str = ""
    emotion: str = ""
    split: bool = True                 # делить реплику на предложения (TTS_SPLIT=sentences)
    split_min_chars: int = MIN_CHARS
    split_max_chars: int = MAX_CHARS
    prefetch: int = 1                  # сколько предложений синтезировать вперёд, пока звучит текущее


@dataclass
class PlaybackMetrics:
    """Задержки и разрывы: от постановки реплики до первого кадра, паузы из-за медленного синтеза."""
    utterances: int = 0
    first_audio_last_ms: int = 0
    first_audio_total_ms: int = 0
    gaps: int = 0
    gap_last_ms: int = 0
    gap_total_ms: int = 0

    def first_audio(self, ms: int) -> None:
        self.utterances += 1
        self.first_audio_last_ms = ms
        self.first_audio_total_ms += ms

    def gap(self, ms: int) -> None:
        self.gaps += 1
        self.gap_last_ms = ms
        self.gap_total_ms += ms

    def public(self) -> dict:
        avg = round(self.first_audio_total_ms / self.utterances) if self.utterances else 0
        return {"utterances": self.utterances, "first_audio_last_ms": self.first_audio_last_ms, "first_audio_avg_ms": avg,
                "gaps": self.gaps, "gap_last_ms": self.gap_last_ms, "gap_total_ms": self.gap_total_ms}


@dataclass
class Sentence:
    text: str
    pcm: bytes | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)       # синтез кончился (успехом или ошибкой)
    failed: bool = False


@dataclass
class Utterance:
    uid: str
    text: str
    sentences: list[Sentence]
    created: float = 0.0
    synth: asyncio.Task | None = None
    kick: asyncio.Event = field(default_factory=asyncio.Event)        # состояние очереди изменилось: можно синтезировать дальше
    sending: int = 0                                                   # индекс предложения, кадры которого уходят сейчас
    play_id: int = 0
    stop_reason: str | None = None
    stop_after: str | None = None                                      # дослушать текущее предложение и остановиться (причина)
    ack: asyncio.Future | None = None                                  # (spoken_ms, reason) от адаптера
    base: float = 0.0                                                  # момент, от которого отсчитывается время кадров
    sent_ms: int = 0                                                   # мс звука, отправленных адаптеру
    timeline: list[tuple[int, int, int, str]] = field(default_factory=list)       # (индекс, начало мс, длина мс, текст)

    @property
    def total_ms(self) -> int:
        return self.sent_ms


@dataclass(frozen=True)
class PlaybackEvent:
    phase: str                          # started | segment | ended
    uid: str
    text: str = ""
    completed: bool = False
    reason: str | None = None
    spoken_ms: int = 0
    total_ms: int = 0
    spoken_text: str = ""
    spoken_segments: int = 0            # целых проигранных предложений
    segment: int = 0                    # у segment: номер предложения в реплике


class Playback:
    """Очередь реплик одной сессии. `transport` даёт `send_control(текст JSON)` и `send_play_frame(play_id, seq, pcm)`."""

    def __init__(self, transport, tts: TtsClient, clock, on_event: Callable[[PlaybackEvent], None],
                 cfg: PlaybackConfig | None = None, play_ids: Callable[[], int] | None = None,
                 sinks: tuple[PlaybackMetrics, ...] = ()):
        self._tr, self._tts, self._clock, self._emit = transport, tts, clock, on_event
        self.cfg = cfg or PlaybackConfig()
        self.metrics = PlaybackMetrics()
        self._sinks = (self.metrics, *sinks)
        self._waiting: deque[Utterance] = deque()
        self._current: Utterance | None = None
        self._play_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._tts_slot = asyncio.Semaphore(1)         # один запрос к сервису синтеза за раз на сессию
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
            self._cancel_synth(cur)
            self._finish(cur, completed=False, reason="closed", spoken_ms=self._estimate(cur))
        self._drop_waiting("closed")

    # ── вход ─────────────────────────────────────────────────────────────────────

    def speak(self, uid: str, text: str, replace: bool = False) -> int:
        """Поставить реплику. Возвращает её место в очереди: 0 звучит первой (или заменила звучащую), далее по порядку."""
        if self._closed:
            raise RuntimeError("сессия закрыта")
        parts = (split_sentences(text, self.cfg.split_min_chars, self.cfg.split_max_chars) if self.cfg.split else [text.strip()])
        parts = [p for p in parts if p]
        if not parts:
            raise ValueError("пустой текст")
        if replace:
            self._drop_waiting("replaced")
            self._interrupt("replaced")
        else:
            while len(self._waiting) >= self.cfg.max_waiting:
                self._discard(self._waiting.popleft(), "replaced")
        utt = Utterance(uid=uid, text=text, sentences=[Sentence(p) for p in parts], created=self._clock.monotonic())
        utt.synth = asyncio.get_running_loop().create_task(self._synth_loop(utt), name=f"voice-tts-{uid}")
        utt.synth.add_done_callback(_consume)
        self._waiting.append(utt)
        self._wake.set()
        self.start()
        if replace:
            return 0
        return len(self._waiting) - 1 + (1 if self._current is not None else 0)

    def stop(self, uid: str | None = None, at: str = "now", reason: str = "stop") -> None:
        """Остановить: uid None это всё (звучащее и очередь). at `now` обрывает звучащее сразу, `segment_end` даёт дослушать
        текущее предложение (остальные отбрасываются); очередь очищается в обоих случаях."""
        if uid is None:
            self._drop_waiting(reason)
            self._stop_current(at, reason)
            return
        for utt in list(self._waiting):
            if utt.uid == uid:
                self._waiting.remove(utt)
                self._discard(utt, reason)
                return
        if self._current is not None and self._current.uid == uid:
            self._stop_current(at, reason)

    def _stop_current(self, at: str, reason: str) -> None:
        cur = self._current
        if cur is None:
            return
        if at == "now" or not self._audible:
            self._interrupt(reason)
        elif cur.stop_after is None and cur.stop_reason is None:
            cur.stop_after = reason

    def barge_in(self) -> bool:
        """Перебивание: оборвать звучащее посреди предложения и очистить очередь. False, если ничего не звучало."""
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

    # ── синтез ───────────────────────────────────────────────────────────────────

    def _limit(self, utt: Utterance) -> int:
        """Наибольший индекс предложения, которое сейчас можно синтезировать: у звучащей реплики текущее плюс предзагрузка,
        у ожидающей только первое (чтобы её старт был без паузы)."""
        return utt.sending + self.cfg.prefetch if utt is self._current else 0

    async def _synth_loop(self, utt: Utterance) -> None:
        for i, sentence in enumerate(utt.sentences):
            while i > self._limit(utt):
                utt.kick.clear()
                await utt.kick.wait()
            try:
                async with self._tts_slot:
                    sentence.pcm = await self._tts.synthesize(sentence.text, voice=self.cfg.voice or None,
                                                              emotion=self.cfg.emotion or None)
            except TtsError as e:
                sentence.failed = True
                logger.warning("Синтез предложения не удался (%s)", type(e).__name__)
            except asyncio.CancelledError:
                raise
            except Exception:
                sentence.failed = True
                logger.exception("Синтез предложения упал")
            sentence.ready.set()

    @staticmethod
    def _cancel_synth(utt: Utterance) -> None:
        if utt.synth is not None:
            utt.synth.cancel()

    # ── внутреннее ───────────────────────────────────────────────────────────────

    def _discard(self, utt: Utterance, reason: str) -> None:
        self._cancel_synth(utt)
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
        text, count = self._spoken(utt, spoken_ms, completed)
        self.last_end_ts = self._clock.time()
        try:
            self._emit(PlaybackEvent("ended", utt.uid, utt.text, completed, None if completed else (reason or "stop"),
                                     spoken_ms, utt.sent_ms, text, count))
        except Exception:
            logger.exception("Обработчик события речи упал")

    @staticmethod
    def _spoken(utt: Utterance, spoken_ms: int, completed: bool) -> tuple[str, int]:
        """Что реально прозвучало: целые проигранные предложения и начало оборванного по доле его звука."""
        parts, whole = [], 0
        for _, start, dur, text in utt.timeline:
            if completed or start + dur <= spoken_ms + FRAME_TOLERANCE_MS:
                parts.append(text)
                whole += 1
            elif spoken_ms > start:
                cut = _cut_text(text, (spoken_ms - start) / dur)
                if cut:
                    parts.append(cut)
                break
            else:
                break
        return " ".join(parts), whole

    def _estimate(self, utt: Utterance) -> int:
        """Сколько мс успело прозвучать по часам (адаптер не подтвердил)."""
        if not utt.base:
            return 0
        elapsed_ms = int((self._clock.monotonic() - utt.base) * 1000)
        return max(0, min(utt.sent_ms, elapsed_ms))

    async def _run(self) -> None:
        while True:
            while not self._waiting:
                self._wake.clear()
                await self._wake.wait()
            utt = self._waiting.popleft()
            self._current = utt
            utt.kick.set()
            self._play_task = asyncio.get_running_loop().create_task(self._play(utt), name=f"voice-play-{utt.uid}")
            try:
                await asyncio.wait({self._play_task})
            except asyncio.CancelledError:
                self._play_task.cancel()
                raise
            task, self._play_task = self._play_task, None
            await self._settle(utt, task)
            self._cancel_synth(utt)
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
        if not utt.base:
            return 0
        fallback = self._estimate(utt)
        try:
            await self._tr.send_control(encode("play.stop", utterance_id=utt.uid, reason=reason))
        except (ConnectionError, RuntimeError, asyncio.TimeoutError):
            return fallback
        got = await self._wait_ack(utt, self.cfg.stop_grace_s)
        return min(utt.sent_ms, got[0]) if got else fallback

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
        """Предложения по очереди: ждём готовый звук, отдаём кадры по 20 мс. Возвращает (сыграно мс, причина)."""
        step, lead = FRAME_MS / 1000.0, self.cfg.lead_ms / 1000.0
        utt.ack = asyncio.get_running_loop().create_future()
        frames_total, stopped_after = 0, None
        last = len(utt.sentences) - 1
        for i, sentence in enumerate(utt.sentences):
            utt.sending = i
            utt.kick.set()
            if utt.stop_after is not None and utt.base:             # segment_end пришёл между предложениями: дальше не идём
                stopped_after = utt.stop_after
                break
            if utt.base:
                due = utt.base + frames_total * step - lead      # следующее предложение нужно к этому моменту
                if due > self._clock.monotonic():
                    await self._clock.sleep(due - self._clock.monotonic())
                if not sentence.ready.is_set():
                    await sentence.ready.wait()
                    late = self._clock.monotonic() - due
                    if late > 0:                                  # синтез не успел: поток ждал, тишина адаптеру не шлётся
                        utt.base += late
                        for sink in self._sinks:
                            sink.gap(int(late * 1000))
                        logger.info("Разрыв в речи: синтез предложения опоздал на %d мс", int(late * 1000))
            else:
                await sentence.ready.wait()
            if sentence.failed or not sentence.pcm:
                continue
            frames = split_frames(sentence.pcm)
            start_ms, dur_ms = utt.sent_ms, pcm_ms(sentence.pcm)
            if not utt.base:
                utt.play_id = self._play_ids()
                await self._tr.send_control(encode("play.start", utterance_id=utt.uid, play_id=utt.play_id,
                                                   text=utt.text[:4000], total_ms=0))
                utt.base = self._clock.monotonic()
                self._audible = True
                self._emit(PlaybackEvent("started", utt.uid, utt.text))
            else:
                self._emit(PlaybackEvent("segment", utt.uid, sentence.text, segment=i))
            utt.timeline.append((i, start_ms, dur_ms, sentence.text))
            for frame in frames:
                delay = utt.base + frames_total * step - lead - self._clock.monotonic()
                if delay > 0:
                    await self._clock.sleep(delay)
                await self._tr.send_play_frame(utt.play_id, frames_total, frame)
                if frames_total == 0:
                    for sink in self._sinks:
                        sink.first_audio(int((self._clock.monotonic() - utt.created) * 1000))
                frames_total += 1
                utt.sent_ms += FRAME_MS
            if utt.stop_after is not None:                         # segment_end: предложение дослушано, остальные отброшены
                stopped_after = utt.stop_after
                if i == last:
                    stopped_after = None
                break
        if not utt.base:
            raise TtsError("ни одно предложение не озвучено")
        await self._tr.send_control(encode("play.end", utterance_id=utt.uid, play_id=utt.play_id))
        remaining = utt.base + frames_total * step - self._clock.monotonic()
        got = await self._wait_ack(utt, max(0.0, remaining) + self.cfg.ack_grace_s)
        if got is None:
            return utt.sent_ms, stopped_after or "completed"
        spoken, reason = got
        if stopped_after and reason == "completed":
            reason = stopped_after
        return min(utt.sent_ms, spoken), reason


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
