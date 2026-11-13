"""Слушатель микрофона: речь пользователя → VAD → локальное распознавание → событие `voice.utterance` (ТЗ 12).

По умолчанию выключен (общая настройка «mic»). Звук идёт только на ЛОКАЛЬНОЕ распознавание: GigaAM на процессоре, запасной
faster-whisper (stt_client, local_stt); в облако не уходит; ничего не пишется на диск. Говорящих не различаем: всякая
распознанная реплика с микрофона считается репликой владельца (`speaker.owner: true`). Пока ГЛаДОС говорит сама, звук с
микрофона отбрасывается (не слушаем собственное эхо из колонок): «ворота» берут состояние «говорит ли она» из воспроизведения
самого модуля (VOICE-7), после речи микрофон закрыт ещё `echo_tail_ms`.

Перебивание (VOICE-10, настройка `barge_in`, по умолчанию выключено): пока она говорит, микрофон не закрывается полностью:
кадры речи копятся отдельно, и когда непрерывная речь длится не меньше 800 мс, этот кусок распознаётся. Если в нём есть
настоящая речь (не пусто и не галлюцинация), её речь обрывается, накопленное уходит в сегментатор, и реплика записывается как
обычная. Голоса не сравниваются, поэтому перебивание надёжно только в наушниках: колонки дадут ложные срабатывания на её эхо.

Логика сегментации и фильтр галлюцинаций перенесены из GitS (src/audio/listener.py).
"""
import asyncio
import logging
import threading
import time

import numpy as np

from gits_sdk import host
from gits_sdk import trace as latency_trace  # метки трассы задержки: SDK, не внутренности ядра (ТЗ 31)
from local_stt import _JUNK

logger = logging.getLogger("mic")

SR = 16000
FRAME = 480                       # 30 мс при 16 кГц: размер кадра для webrtcvad
FRAME_BYTES = FRAME * 2
SILENCE_FRAMES = 17               # ~0,5 с тишины = конец реплики (меньше ~0,4 с режет фразы с паузой)
MIN_SPEECH_FRAMES = 7             # короче ~0,2 с: щелчок, не речь
MAX_FRAMES = 660                  # ~20 с: дольше реплику не копим
ENERGY_RMS = 600                  # порог громкости, если webrtcvad недоступен
ECHO_TAIL = 0.6                   # сек после конца её реплики микрофон ещё «закрыт» (по умолчанию; свой: echo_tail_ms)
SPEAKING_MIN_INTERVAL_S = 0.2     # VOICE-8: voice.user_speaking не чаще 5 раз в секунду
BARGE_MIN_FRAMES = 27             # VOICE-10: ~800 мс непрерывной речи, чтобы проверить голос
BARGE_MAX_GAP_FRAMES = 4          # короткая пауза внутри фразы (до ~120 мс) речь не прерывает
BARGE_MAX_FRAMES = 330            # ~10 с: дольше копить без решения незачем
BARGE_RETRY_S = 2.0               # после «это её голос» новую проверку не раньше, чем через столько секунд

# Говорящий реплики с микрофона: всегда владелец (событие voice.utterance, поле `speaker`)
OWNER_SPEAKER = {"id": "owner", "name": None, "owner": True}

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

    @property
    def active(self) -> bool:
        """Идёт ли сейчас речь (реплика копится, тишина ещё не выдержана)."""
        return self._active

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


class BargeInDetector:
    """Копит кадры, пока она говорит (VOICE-10), и сообщает, когда набралось ≥ 800 мс непрерывной речи. Чистая логика."""

    def __init__(self, min_frames: int = BARGE_MIN_FRAMES, max_gap: int = BARGE_MAX_GAP_FRAMES,
                 max_frames: int = BARGE_MAX_FRAMES):
        self.min, self.max_gap, self.max_frames = min_frames, max_gap, max_frames
        self.reset()

    def reset(self) -> None:
        self.frames: list[bytes] = []
        self.speech = 0
        self._gap = 0

    def feed(self, frame: bytes, is_speech: bool) -> bool:
        """Добавить кадр; True, когда речи набралось достаточно для проверки голоса."""
        if is_speech:
            self.speech += 1
            self._gap = 0
            self.frames.append(frame)
        elif self.frames:
            self._gap += 1
            if self._gap > self.max_gap:
                self.reset()                                  # пауза длиннее: это уже новая фраза
                return False
            self.frames.append(frame)
        if len(self.frames) > self.max_frames:
            self.reset()
            return False
        return self.speech >= self.min

    def audio(self) -> bytes:
        return b"".join(self.frames)


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
    def __init__(self, stt, on_voice, is_speaking, on_speaking=None, *,
                 settings_get=lambda: {}, echo_tail=lambda: ECHO_TAIL, barge_enabled=lambda: False, on_barge_in=None,
                 vad=None, clock=time.time):
        """stt - STTClient; on_voice(text, info) - реплика распознана (info: говорящий-владелец); is_speaking() - звучит ли она
        (воспроизведение модуля); on_speaking(bool) - человек начал или закончил говорить (VOICE-8, не чаще 5 раз в
        секунду); settings_get() - общие настройки ядра (`mic`); echo_tail() - секунд закрытого микрофона после её речи;
        barge_enabled() - включено ли перебивание; on_barge_in() - обрыв её речи (async); vad(frame) - речь ли кадр."""
        self._stt, self._on_voice, self._is_speaking = stt, on_voice, is_speaking
        self._on_speaking = on_speaking
        self._settings_get = settings_get
        self._echo_tail, self._barge_enabled, self._on_barge_in = echo_tail, barge_enabled, on_barge_in
        self._vad = vad
        self._clock = clock
        self._user_speaking = False                    # что уже сообщено наружу
        self._want_speaking = False                    # что должно быть сообщено (ждёт паузы между сообщениями)
        self._last_speaking_ts = float("-inf")
        self._warned = 0.0
        self._tail_until = 0.0
        self._seg: UtteranceSegmenter | None = None
        self._detector = BargeInDetector()
        self._barge_state: str | None = None           # None | "checking" | "confirmed"
        self._barge_block_until = 0.0
        self._tasks: set[asyncio.Task] = set()

    def _enabled(self) -> bool:
        return bool(self._settings_get().get("mic"))

    def prepare(self, vad=None) -> None:
        """Новый сегментатор и детектор перебивания (начало сессии записи; в тестах: подставить свой VAD)."""
        vad = vad or self._vad or Vad().is_speech
        self._vad = vad
        self._seg = UtteranceSegmenter(vad)
        self._detector.reset()
        self._barge_state = None

    def _gate(self) -> bool:
        """Микрофон закрыт, пока она говорит, и недолго после."""
        now = self._clock()
        if self._is_speaking():
            self._tail_until = now + self._echo_tail()
            return True
        return now < self._tail_until

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
                    host.log_nowait("error", f"микрофон: {e}")
                await asyncio.sleep(30)

    async def _session(self):
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        stop = threading.Event()

        def push(b):
            if not q.full():
                q.put_nowait(b)

        threading.Thread(target=_record_thread, args=(stop, push, loop), daemon=True, name="mic").start()
        self.prepare()
        host.log_nowait("hearing", "микрофон включён: слушаю речь")
        try:
            while self._enabled():
                try:
                    frame = await asyncio.wait_for(q.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                await self.handle_frame(frame)
        finally:
            stop.set()
            self._note_speaking(False, force=True)
            host.log_nowait("hearing", "микрофон выключен")

    # ── разбор кадра ─────────────────────────────────────────────────────────

    async def handle_frame(self, frame: bytes) -> None:
        """Один кадр записи: обычный путь (сегментатор), накопление для перебивания или продолжение после него."""
        if self._seg is None:
            self.prepare()
        seg = self._seg
        self.flush_speaking()
        if self._barge_state == "confirmed":
            done = seg.feed(frame, False)                      # её речь оборвана: запись реплики идёт как обычная
            self._note_speaking(seg.active)
            if done is not None or not seg.active:
                self._barge_state = None
            if done is not None:
                self._launch_utterance(done[0])
            return
        if self._barge_enabled() and self._is_speaking():
            self._note_speaking(False)
            if self._clock() >= self._barge_block_until and self._detector.feed(frame, self._vad(frame)) \
                    and self._barge_state is None:
                self._barge_state = "checking"
                self._spawn_barge(self._detector.audio())
            return
        if self._barge_state == "checking":
            self._barge_state = None                           # она замолчала сама, пока шла проверка
        if self._detector.frames:
            self._detector.reset()
        gated = self._gate()
        done = seg.feed(frame, gated)
        self._note_speaking(seg.active and not gated)          # её собственный голос из колонок речью пользователя не считается
        if done is None:
            return
        audio, overlap = done
        if not overlap and not self._gate():
            self._launch_utterance(audio)

    def _launch_utterance(self, audio: bytes) -> None:
        # трасса реплики (ТЗ 24, TRC-1): конец фразы - первая метка, дальше её ведёт распознавание и ядро
        wire = latency_trace.wire_new("voice", marks={"mic_end": latency_trace.now_ms()})
        task = asyncio.get_running_loop().create_task(self.handle_utterance(audio, wire))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_barge(self, audio: bytes) -> None:
        task = asyncio.get_running_loop().create_task(self._check_barge(audio))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _check_barge(self, audio: bytes) -> None:
        """VOICE-10: есть ли в накопленном куске настоящая речь. Есть: оборвать её речь (голоса не сравниваем)."""
        interrupt = False
        try:
            text = (await self._stt.transcribe(audio)).text.strip()
            interrupt = bool(text) and not is_hallucination(text)
        except Exception as e:
            logger.debug(f"Проверка речи для перебивания не удалась: {e}")
        if not interrupt or self._barge_state != "checking" or not self._is_speaking():
            if self._barge_state == "checking":
                self._barge_state = None
            self._detector.reset()
            self._barge_block_until = self._clock() + BARGE_RETRY_S
            return
        self._barge_state = "confirmed"
        self._tail_until = 0.0                                 # она замолчит: ждать эхо незачем
        frames = list(self._detector.frames)
        self._detector.reset()
        for f in frames:                                       # накопленное продолжает запись реплики (до новых кадров)
            self._seg.feed(f, False)
        self._note_speaking(self._seg.active)
        if self._on_barge_in is not None:
            try:
                await self._on_barge_in()
            except Exception as e:
                logger.warning(f"Обрыв речи при перебивании не удался: {e}")

    # ── признак «пользователь говорит» ───────────────────────────────────────

    def _note_speaking(self, speaking: bool, force: bool = False) -> None:
        """VOICE-8: сообщить о смене не чаще раза в 0,2 с (отложенная смена уходит на ближайшем кадре)."""
        self._want_speaking = speaking
        if self._on_speaking is None or self._want_speaking == self._user_speaking:
            return
        now = self._clock()
        if not force and now - self._last_speaking_ts < SPEAKING_MIN_INTERVAL_S:
            return
        self._user_speaking, self._last_speaking_ts = self._want_speaking, now
        try:
            self._on_speaking(self._user_speaking)
        except Exception as e:
            logger.debug(f"Признак «пользователь говорит»: {e}")

    # ── реплика ──────────────────────────────────────────────────────────────

    async def handle_utterance(self, audio: bytes, trace: dict | None = None):
        """Расшифровать реплику и передать наружу (вынесено для проверки без микрофона). trace - метки трассы (ТЗ 24)."""
        try:
            t = await self._stt.transcribe(audio)
            if trace is not None:
                trace["marks"]["stt_done"] = latency_trace.now_ms()
        except Exception as e:
            # состояние модели распознавания уходит в состояние модуля и журнал отдельно (VOICE, STT)
            if time.time() - self._warned > 120:
                self._warned = time.time()
                host.log_nowait("error", f"микрофон: распознавание не удалось ({type(e).__name__}: {str(e)[:80]})")
            return
        text = t.text.strip()
        if not text or is_hallucination(text):
            return
        info = dict(OWNER_SPEAKER)                   # говорящих не различаем: микрофон принадлежит владельцу
        if trace is not None:
            await self._on_voice(text, info, trace=trace)
        else:
            await self._on_voice(text, info)

    def flush_speaking(self) -> None:
        """Отложенная смена признака «говорит» уходит, когда пауза между сообщениями прошла (вызывается на каждом кадре)."""
        if self._want_speaking != self._user_speaking:
            self._note_speaking(self._want_speaking)
