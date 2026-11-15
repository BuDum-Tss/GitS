"""Движок голоса: фасад над сессиями, сегментацией речи, распознаванием, очередью реплик и перебиванием.

Вход (звук говорящих) -> VAD и сегментатор по каждому говорящему -> STT -> событие `voicechat.utterance` менеджера внимания.
Выход (`speak`) -> TTS -> кадры по 20 мс адаптеру -> событие `ended` -> журнал `voicechat.spoken` для памяти.

Движок ничего не знает о ядре: события уходят через переданные функции `emit_event` (в ядре это `attention.submit`),
`journal` и `on_speech`. Сетевых соединений тоже не знает: адаптер для него это `SessionTransport` (в ядре это WebSocket,
`server.py`; в тестах заглушка).
"""
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from . import events
from .clock import SystemClock
from .config import EngineConfig
from .playback import Playback, PlaybackConfig, PlaybackEvent, PlaybackMetrics
from .session import Capabilities, SessionLimitError, SessionManager, SessionTransport, Speaker, VoiceSession
from .stt import SttClient, SttError
from .tts import TtsClient, clean_text
from .vad import EnergyVad, Segment, SegmenterConfig, UtteranceSegmenter, VadDetector

logger = logging.getLogger("voice_engine")

NOISE_MIN_CHARS = 2                      # распознанное короче (одна буква, «э») пропускается
INTERRUPT_MEMORY_S = 15.0                # фраза в течение стольких секунд после перебивания помечается interrupted_me


@dataclass(frozen=True)
class SpeakTicket:
    uid: str
    session: str
    position: int                        # 0: звучит сейчас или следующей


class VoiceEngine:
    def __init__(self, *, stt: SttClient, tts: TtsClient, config: EngineConfig | None = None, clock=None,
                 emit_event: Callable[[dict], Awaitable[None]] | None = None,
                 journal: Callable[[str, str, dict], None] | None = None,
                 on_speech: Callable[[dict], None] | None = None,
                 names: Callable[[], Iterable[str]] | None = None,
                 vad_factory: Callable[[], VadDetector] | None = None):
        self.config = config or EngineConfig()
        self.stt, self.tts = stt, tts
        self.clock = clock or SystemClock()
        self._emit_event, self._journal, self._on_speech = emit_event, journal, on_speech
        self._names = names or (lambda: ())
        cfg = self.config
        self._vad_factory = vad_factory or (lambda: EnergyVad(cfg.energy_threshold_db))
        self._segmenter_cfg = SegmenterConfig(onset_ms=cfg.onset_ms, min_speech_ms=cfg.min_speech_ms, tail_ms=cfg.tail_ms,
                                              max_ms=cfg.max_utterance_ms)
        self.sessions_manager = SessionManager(cfg.max_sessions, cfg.max_speakers)
        self._stt_sem = asyncio.Semaphore(cfg.stt_concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._patterns_key: tuple = ()
        self._patterns: list = []
        self._play_counter = 0
        self.metrics = PlaybackMetrics()                  # задержка первого звука и разрывы по всем сессиям
        self.started_at = time.time()

    # ── сессии ───────────────────────────────────────────────────────────────────

    def resolve_barge_in(self, caps: Capabilities, requested: str | None = None) -> str:
        """Политика перебивания сессии. `any_speaker` только для адаптера без эха (`echo_free`) и с мгновенной остановкой:
        иначе её собственный голос из колонок слышен микрофону и она перебивала бы сама себя."""
        wanted = requested or self.config.barge_in
        safe = caps.echo_free and caps.can_barge_in
        if wanted == "off":
            return "off"
        return "any_speaker" if safe else "off"

    async def open_session(self, session_id: str, adapter: str, transport: SessionTransport,
                           caps: Capabilities | None = None, *, barge_in: str | None = None) -> VoiceSession:
        caps = caps or Capabilities()
        sess = VoiceSession(session_id, adapter, caps, transport, self.resolve_barge_in(caps, barge_in), self.config.barge_in_ms)
        sess.playback = Playback(transport, self.tts, self.clock, lambda ev: self._on_playback(sess, ev),
                                 PlaybackConfig(max_waiting=self.config.max_waiting, lead_ms=self.config.lead_ms,
                                                voice=self.config.tts_voice, emotion=self.config.tts_emotion,
                                                split=self.config.tts_split == "sentences", prefetch=self.config.tts_prefetch,
                                                split_min_chars=self.config.split_min_chars,
                                                split_max_chars=self.config.split_max_chars),
                                 play_ids=self._next_play_id, sinks=(self.metrics,))
        old = self.sessions_manager.add(sess)             # SessionLimitError пробрасывается адаптеру отказом
        if old is not None:
            await self._shutdown(old, "replaced", close_transport=True)
        logger.info("Голосовая сессия %s открыта (адаптер %s, перебивание: %s)", session_id, adapter, sess.barge_in_policy)
        return sess

    async def close_session(self, session_id: str, reason: str = "closed", *, transport: SessionTransport | None = None) -> bool:
        """Закрыть сессию. Если указан `transport`, закрывается только сессия именно этого соединения (после переподключения
        адаптера старое соединение не должно закрыть новую сессию с тем же id)."""
        sess = self.sessions_manager.get(session_id)
        if sess is None or (transport is not None and sess.transport is not transport):
            return False
        self.sessions_manager.remove(sess)
        await self._shutdown(sess, reason, close_transport=transport is None)
        logger.info("Голосовая сессия %s закрыта (%s)", session_id, reason)
        return True

    async def _shutdown(self, sess: VoiceSession, reason: str, *, close_transport: bool) -> None:
        sess.closed = True
        for task in list(sess.tasks):
            task.cancel()
        if sess.playback is not None:
            await sess.playback.aclose()
        if close_transport:
            try:
                await asyncio.wait_for(sess.transport.close(reason), 3.0)
            except (ConnectionError, RuntimeError, asyncio.TimeoutError):
                pass

    def session(self, session_id: str) -> VoiceSession | None:
        return self.sessions_manager.get(session_id)

    def sessions(self) -> list[dict]:
        return [s.public() for s in self.sessions_manager.all()]

    def _next_play_id(self) -> int:
        self._play_counter = self._play_counter % 0xFFFFFFFF + 1
        return self._play_counter

    # ── говорящие и звук ─────────────────────────────────────────────────────────

    def add_speaker(self, session_id: str, speaker_id: str, name: str, idx: int, *, is_owner: bool = False,
                    is_bot: bool = False) -> Speaker:
        sess = self._open(session_id)
        sp = sess.speakers.get(speaker_id)
        if sp is None:
            if len(sess.speakers) >= self.sessions_manager.max_speakers:
                raise SessionLimitError(f"в сессии {len(sess.speakers)} говорящих из {self.sessions_manager.max_speakers} возможных")
            if idx in sess.by_idx:
                raise ValueError(f"номер говорящего {idx} занят")
            sp = Speaker(speaker_id, name or speaker_id, idx, UtteranceSegmenter(self._vad_factory(), self._segmenter_cfg))
            sess.speakers[speaker_id] = sp
            sess.by_idx[idx] = sp
        sp.name, sp.is_owner, sp.is_bot = name or sp.name, is_owner, is_bot
        return sp

    def remove_speaker(self, session_id: str, speaker_id: str) -> None:
        sess = self.sessions_manager.get(session_id)
        sp = sess.speakers.pop(speaker_id, None) if sess is not None else None
        if sp is None or sess is None:
            return
        sess.by_idx.pop(sp.idx, None)
        tail = sp.segmenter.flush()               # недосказанная фраза ушедшего всё равно считается
        if tail is not None:
            self._submit_segment(sess, sp, tail)

    def hint_speaking(self, session_id: str, speaker_id: str, active: bool) -> None:
        sess = self.sessions_manager.get(session_id)
        sp = sess.speakers.get(speaker_id) if sess is not None else None
        if sp is not None:
            sp.speaking_hint = active

    def feed(self, session_id: str, speaker: str | int, pcm: bytes) -> bool:
        """Звук говорящего (PCM s16le 16 кГц моно, любой длины): `speaker` это его id или номер idx из кадра. False: сессии
        или говорящего нет, звук отброшен."""
        sess = self.sessions_manager.get(session_id)
        if sess is None or sess.closed:
            return False
        sp = sess.by_idx.get(speaker) if isinstance(speaker, int) else sess.speakers.get(speaker)
        if sp is None:
            sess.stats.bad_frames += 1
            return False
        sess.stats.frames_in += 1
        sp.frames += 1
        for seg in sp.segmenter.push(pcm):
            self._submit_segment(sess, sp, seg)
        self._maybe_barge_in(sess, sp)
        return True

    def _maybe_barge_in(self, sess: VoiceSession, sp: Speaker) -> None:
        pb = sess.playback
        if (sess.barge_in_policy == "any_speaker" and pb is not None and pb.audible and not sp.is_bot
                and sp.segmenter.run_ms >= sess.barge_in_ms and pb.barge_in()):
            sess.stats.barge_ins += 1
            sp.barged_ts = self.clock.time()

    # ── распознавание ────────────────────────────────────────────────────────────

    def _submit_segment(self, sess: VoiceSession, sp: Speaker, seg: Segment) -> None:
        if sess.stt_pending >= self.config.stt_pending_per_session:
            sess.stats.dropped_utterances += 1
            logger.warning("Распознавание не успевает: фраза говорящего отброшена (сессия %s)", sess.id)
            return
        sess.stt_pending += 1
        self._spawn(sess, self._transcribe(sess, sp, seg), f"voice-stt-{sess.id}")

    def _spawn(self, sess: VoiceSession, coro, name: str) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        sess.tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(sess.tasks.discard)
        return task

    async def _transcribe(self, sess: VoiceSession, sp: Speaker, seg: Segment) -> None:
        try:
            async with sp.stt_lock:                     # фразы одного говорящего идут по порядку
                async with self._stt_sem:
                    result = await self.stt.transcribe(seg.pcm, lang=self.config.lang)
        except SttError as e:
            sess.stats.stt_errors += 1
            logger.warning("Распознавание не удалось (%s): %s", type(e).__name__, e)
            return
        finally:
            sess.stt_pending -= 1
        text = result.text.strip()
        if sess.closed or len(text.strip(" .,!?-—…")) < NOISE_MIN_CHARS:
            return
        speaker = sp.public()
        addressed, reason = self._addressed(sess, text)
        interrupted = sp.barged_ts > 0 and self.clock.time() - sp.barged_ts < INTERRUPT_MEMORY_S
        event = events.utterance_event(text=text, speaker=speaker, session=sess.id, adapter=sess.adapter,
                                       duration_ms=result.duration_ms or seg.duration_ms, language=result.language or self.config.lang,
                                       addressed=addressed, reason=reason, interrupted=interrupted)
        sess.stats.utterances += 1
        if self._emit_event is not None:
            try:
                await self._emit_event(event)
            except Exception:
                logger.exception("Событие голосовой фразы не принято ядром")

    def _addressed(self, sess: VoiceSession, text: str) -> tuple[bool, str]:
        key = tuple(self._names())
        if key != self._patterns_key:
            self._patterns_key, self._patterns = key, events.name_patterns(key)
        if events.is_addressed(text, self._patterns):
            return True, "name"
        if sess.one_to_one:
            return True, "one_to_one"
        return False, ""

    # ── речь ─────────────────────────────────────────────────────────────────────

    def _open(self, session_id: str) -> VoiceSession:
        sess = self.sessions_manager.get(session_id)
        if sess is None or sess.closed:
            raise KeyError(f"голосовой сессии «{session_id}» нет")
        return sess

    def speak(self, session_id: str, text: str, *, replace: bool = False, uid: str | None = None) -> SpeakTicket:
        """Поставить реплику в очередь сессии: не ждёт синтеза и звука. Конец речи приходит событием `ended` (журнал
        `voicechat.spoken`, хук `on_speech`)."""
        sess = self._open(session_id)
        text = clean_text(text)
        if not text:
            raise ValueError("пустой текст")
        uid = uid or "u-" + secrets.token_hex(4)
        position = sess.playback.speak(uid, text, replace=replace)
        return SpeakTicket(uid, sess.id, position)

    def stop(self, session_id: str, at: str = "now", uid: str | None = None, reason: str = "stop") -> None:
        sess = self._open(session_id)
        sess.playback.stop(uid, at, reason)

    def ack(self, session_id: str, uid: str, spoken_ms: int, reason: str) -> None:
        sess = self.sessions_manager.get(session_id)
        if sess is not None and sess.playback is not None:
            sess.playback.on_ack(uid, spoken_ms, reason)

    def _on_playback(self, sess: VoiceSession, ev: PlaybackEvent) -> None:
        if ev.phase == "ended" and ev.spoken_ms > 0:
            sess.stats.spoken += 1
        if ev.phase == "ended":
            if self._journal is not None and ev.spoken_text:
                self._journal("module_out", ev.spoken_text[:events.EVENT_TEXT_MAX], {
                    "module": events.PEER_ID, "tool": "voice.say", "to": sess.id, "own": True, "event_kind": events.KIND_SPOKEN,
                    "adapter": sess.adapter, "utterance_id": ev.uid, "completed": ev.completed, "reason": ev.reason,
                    "spoken_ms": ev.spoken_ms, "spoken_segments": ev.spoken_segments, "interrupted": ev.reason == "barge_in"})
        if self._on_speech is not None:
            data: dict[str, Any] = {"phase": ev.phase, "utterance_id": ev.uid, "session": sess.id, "adapter": sess.adapter}
            if ev.phase == "segment":
                data.update(seq=ev.segment, segment=ev.text)
            if ev.phase == "ended":
                data.update(completed=ev.completed, spoken_upto=ev.spoken_segments, spoken_ms=ev.spoken_ms, total_ms=ev.total_ms,
                            spoken_text=ev.spoken_text)
                if ev.reason:
                    data["reason"] = ev.reason
            try:
                self._on_speech(data)
            except Exception:
                logger.exception("Обработчик событий речи упал")

    # ── состояние и остановка ────────────────────────────────────────────────────

    async def settle(self) -> None:
        """Дождаться начатых распознаваний (тесты, остановка)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def snapshot(self) -> dict:
        return {"enabled": True, "uptime_s": round(time.time() - self.started_at, 1), "sessions": self.sessions(),
                "limits": {"max_sessions": self.sessions_manager.max_sessions, "max_speakers": self.sessions_manager.max_speakers},
                "metrics": self.metrics.public(), "tts_split": self.config.tts_split, "tts_prefetch": self.config.tts_prefetch,
                "stt_url": self.config.stt_url, "tts_url": self.config.tts_url, "lang": self.config.lang}

    async def aclose(self) -> None:
        for sess in self.sessions_manager.all():
            await self.close_session(sess.id, "engine_stop")
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)
        await self.stt.aclose()
        await self.tts.aclose()

