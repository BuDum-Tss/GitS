"""Слушатель микрофона: речь пользователя → VAD → WhisperX → узнавание голоса → в разговор с ГЛаДОС.

По умолчанию выключен (настройка «mic»). Звук идёт только на ЛОКАЛЬНЫЙ сервер WhisperX (docker compose up whisperx),
в облако не уходит; ничего не пишется на диск. Пока ГЛаДОС говорит сама, звук с микрофона отбрасывается (не слушаем
собственное эхо из колонок); голос «self» из voice_id служит вторым рубежом.
Логика сегментации и фильтр галлюцинаций перенесены из GitS (src/audio/listener.py).
"""
import asyncio
import logging
import threading
import time

import numpy as np

import events_log
import settings
from hearing import _JUNK
from voice_id import SELF_PERSON_ID

logger = logging.getLogger("mic")

SR = 16000
FRAME = 480                       # 30 мс при 16 кГц: размер кадра для webrtcvad
FRAME_BYTES = FRAME * 2
SILENCE_FRAMES = 17               # ~0,5 с тишины = конец реплики (меньше ~0,4 с режет фразы с паузой)
MIN_SPEECH_FRAMES = 7             # короче ~0,2 с: щелчок, не речь
MAX_FRAMES = 660                  # ~20 с: дольше реплику не копим
ENERGY_RMS = 600                  # порог громкости, если webrtcvad недоступен
ECHO_TAIL = 0.6                   # сек после конца её реплики микрофон ещё «закрыт»

_EXACT_JUNK = ("продолжение следует",)


def is_hallucination(text: str) -> bool:
    """Типичные выдумки Whisper на шуме и тишине."""
    low = text.lower().strip(".,!? ")
    return not low or low in _EXACT_JUNK or bool(_JUNK.search(text))


class Vad:
    """Речь или нет для кадра в 30 мс: webrtcvad, а без него по громкости."""

    def __init__(self, aggressiveness: int = 2):
        try:
            import webrtcvad
            self._v = webrtcvad.Vad(aggressiveness)
        except Exception:
            self._v = None

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != FRAME_BYTES:
            return False
        if self._v is not None:
            try:
                return self._v.is_speech(frame, SR)
            except Exception:
                return False
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(arr ** 2))) > ENERGY_RMS


class UtteranceSegmenter:
    """Копит кадры речи и отдаёт целую реплику, когда наступила тишина. Чистая логика: легко проверяется."""

    def __init__(self, is_speech, silence_frames: int = SILENCE_FRAMES, min_speech: int = MIN_SPEECH_FRAMES,
                 max_frames: int = MAX_FRAMES):
        self._is_speech = is_speech
        self._silence_n, self._min, self._max = silence_frames, min_speech, max_frames
        self._reset()

    def _reset(self):
        self._frames: list[bytes] = []
        self._speech = 0
        self._silence = 0
        self._active = False
        self._overlap = False

    def feed(self, frame: bytes, agent_speaking: bool = False):
        """Вернуть (аудио, пересеклось_ли_с_её_речью), когда реплика закончилась, иначе None."""
        if self._is_speech(frame):
            self._active, self._silence = True, 0
            self._speech += 1
            self._frames.append(frame)
            self._overlap = self._overlap or agent_speaking
        elif self._active:
            self._frames.append(frame)
            self._silence += 1
        else:
            return None
        if self._silence >= self._silence_n or len(self._frames) >= self._max:
            audio, ok, overlap = b"".join(self._frames), self._speech >= self._min, self._overlap
            self._reset()
            return (audio, overlap) if ok else None
        return None


def _record_thread(stop: threading.Event, push, loop):
    """Поток записи микрофона (soundcard): кадры по 30 мс уходят в asyncio-очередь."""
    import soundcard as sc
    mic = sc.default_microphone()
    with mic.recorder(samplerate=SR, channels=1, blocksize=FRAME * 4) as rec:
        while not stop.is_set():
            data = rec.record(numframes=FRAME)
            pcm = (np.clip(data[:, 0], -1, 1) * 32767).astype(np.int16).tobytes()
            loop.call_soon_threadsafe(push, pcm)


class MicListener:
    def __init__(self, stt, voice_id, on_voice, is_speaking, make_self_sample=None):
        self._stt, self._voices, self._on_voice, self._is_speaking = stt, voice_id, on_voice, is_speaking
        self._make_self_sample = make_self_sample      # () -> PCM 16 кГц её голоса, для регистрации «self»
        self._warned = 0.0
        self._tail_until = 0.0

    def _enabled(self) -> bool:
        return bool(settings.load().get("mic"))

    def _gate(self) -> bool:
        """Микрофон закрыт, пока она говорит, и недолго после."""
        if self._is_speaking():
            self._tail_until = time.time() + ECHO_TAIL
            return True
        return time.time() < self._tail_until

    async def run(self):
        while True:
            if not self._enabled():
                await asyncio.sleep(2)
                continue
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if time.time() - self._warned > 300:
                    self._warned = time.time()
                    events_log.log("error", f"микрофон: {e}")
                await asyncio.sleep(30)

    async def _session(self):
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        stop = threading.Event()

        def push(b):
            if not q.full():
                q.put_nowait(b)

        threading.Thread(target=_record_thread, args=(stop, push, loop), daemon=True, name="mic").start()
        seg = UtteranceSegmenter(Vad().is_speech)
        await self._ensure_self_voice()
        events_log.log("hearing", "микрофон включён: слушаю речь")
        try:
            while self._enabled():
                try:
                    frame = await asyncio.wait_for(q.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                done = seg.feed(frame, self._gate())
                if done is None:
                    continue
                audio, overlap = done
                if not overlap and not self._gate():
                    asyncio.create_task(self.handle_utterance(audio))
        finally:
            stop.set()
            events_log.log("hearing", "микрофон выключен")

    async def _ensure_self_voice(self):
        """Зарегистрировать голос самой ГЛаДОС по образцу синтеза (если WhisperX уже умеет эмбеддинги)."""
        if self._voices.has_self() or self._make_self_sample is None:
            return
        try:
            pcm = await self._make_self_sample()
            t = await self._stt.transcribe(pcm, diarize=True) if pcm else None
            if t and t.speaker_embedding:
                self._voices.register_self(t.speaker_embedding)
                events_log.log("hearing", "голос ГЛаДОС записан: собственное эхо игнорируется")
        except Exception as e:
            logger.info(f"Свой голос не записан: {e}")

    async def handle_utterance(self, audio: bytes):
        """Расшифровать реплику и передать в разговор (вынесено для проверки без микрофона)."""
        try:
            t = await self._stt.transcribe(audio)
        except Exception as e:
            if time.time() - self._warned > 120:
                self._warned = time.time()
                events_log.log("error", f"микрофон: сервер WhisperX не отвечает ({str(e)[:80]}): docker compose up whisperx")
            return
        text = t.text.strip()
        if not text or is_hallucination(text):
            return
        pid, new = self._voices.resolve(t.speaker_embedding)
        if pid == SELF_PERSON_ID:
            return                                   # это её собственный голос из колонок
        info = {"id": pid or t.speaker, "name": self._voices.name_of(pid) if pid else None, "new": new,
                "owner": bool(pid) and pid == self._voices.owner(), "has_owner": self._voices.owner() is not None}
        await self._on_voice(text, info)
