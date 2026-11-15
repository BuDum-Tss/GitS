"""Определение речи и нарезка потока на реплики: отдельно по каждому говорящему (диаризация не нужна: Discord и шлюз ПК
присылают поток каждого участника отдельно).

`VadDetector` решает для одного кадра в 20 мс, речь это или нет. `EnergyVad` (по уровню сигнала) работает без моделей и
годится по умолчанию; `SileroVad` точнее на шуме, но нужен onnxruntime и файл модели (если их нет, берётся `EnergyVad`).
`UtteranceSegmenter` превращает поток кадров в законченные реплики: начало после короткой устойчивой речи, конец после хвоста
тишины, короткие щелчки отбрасываются, слишком длинная реплика режется.
"""
import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np

from .frames import BYTES_PER_MS, FRAME_BYTES, FRAME_MS, SAMPLE_RATE, rms_db

logger = logging.getLogger("voice_engine.vad")


class VadDetector(Protocol):
    """Решение по одному кадру (640 байт). Объект хранит состояние одного говорящего: свой экземпляр на каждого."""

    def is_speech(self, pcm: bytes) -> bool: ...

    def reset(self) -> None: ...


class EnergyVad:
    """Речь, если уровень кадра не ниже порога (dBFS). Дёшево: один проход numpy по 320 отсчётам."""

    def __init__(self, threshold_db: float = -42.0):
        self.threshold_db = threshold_db

    def is_speech(self, pcm: bytes) -> bool:
        return rms_db(pcm) >= self.threshold_db

    def reset(self) -> None:
        pass


class SileroVad:
    """Silero VAD (onnx): окна по 512 отсчётов, кадр в 320 отсчётов копится до целого окна, решение держится до следующего.

    Состояние сети хранится в экземпляре, поэтому на каждого говорящего свой. Сессия onnxruntime общая (потокобезопасна)."""

    WINDOW = 512
    CONTEXT = 64

    def __init__(self, session, threshold: float = 0.5):
        self._session = session
        self.threshold = threshold
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self.CONTEXT), dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)
        self._last = 0.0

    def is_speech(self, pcm: bytes) -> bool:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._pending = np.concatenate((self._pending, samples))
        while len(self._pending) >= self.WINDOW:
            window, self._pending = self._pending[:self.WINDOW], self._pending[self.WINDOW:]
            data = np.concatenate((self._context, window[None, :]), axis=1).astype(np.float32)
            out, self._state = self._session.run(None, {"input": data, "state": self._state,
                                                        "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
            self._context = data[:, -self.CONTEXT:]
            self._last = float(out.reshape(-1)[0])
        return self._last >= self.threshold


def load_silero_factory(model_path: str, threshold: float = 0.5) -> Callable[[], VadDetector] | None:
    """Фабрика детекторов Silero или None, если onnxruntime не установлен, файла нет или модель не открылась."""
    if not model_path:
        return None
    try:
        import onnxruntime as ort
    except ImportError:
        logger.warning("Silero VAD запрошен, но onnxruntime не установлен: берётся EnergyVad")
        return None
    try:
        options = ort.SessionOptions()
        options.inter_op_num_threads = options.intra_op_num_threads = 1
        session = ort.InferenceSession(model_path, sess_options=options, providers=["CPUExecutionProvider"])
    except Exception as e:                       # битый или отсутствующий файл: не роняем движок
        logger.warning("Silero VAD не открылся (%s): берётся EnergyVad", type(e).__name__)
        return None
    return lambda: SileroVad(session, threshold)


@dataclass(frozen=True)
class SegmenterConfig:
    onset_ms: int = 60              # столько речи подряд, чтобы считать реплику начавшейся
    min_speech_ms: int = 240        # реплики с меньшим количеством речи отбрасываются (щелчок, вдох)
    tail_ms: int = 600              # тишина после речи, закрывающая реплику
    max_ms: int = 30000             # длиннее реплика режется (STT и память не любят бесконечные куски)
    preroll_ms: int = 200           # сколько звука до начала речи добавить реплике (первый слог не обрезается)
    keep_tail_ms: int = 200         # сколько тишины оставить в конце реплики


@dataclass(frozen=True)
class Segment:
    """Законченная реплика одного говорящего."""
    pcm: bytes
    duration_ms: int
    speech_ms: int
    forced: bool = False            # закрыта принудительно (превышена длина или ушёл говорящий)


class UtteranceSegmenter:
    """Поток одного говорящего -> реплики. `push` принимает PCM любой длины (кратной отсчёту), копит неполный кадр."""

    RUN_GAP_MS = 100                # пауза короче этой не обрывает «устойчивую речь» (для перебивания)

    def __init__(self, vad: VadDetector, cfg: SegmenterConfig | None = None, loud_db: float | None = None):
        self.vad = vad
        self.loud_db = loud_db          # порог громкости для перебивания (None: не считать)
        self._loud_ms = 0
        self._loud_gap = 0
        self.cfg = cfg or SegmenterConfig()
        self._buf = bytearray()
        self._preroll: deque[bytes] = deque(maxlen=max(1, (self.cfg.preroll_ms + self.cfg.onset_ms) // FRAME_MS))
        self._frames: list[bytes] = []
        self._speaking = False
        self._onset = 0
        self._voiced_ms = 0
        self._silence_ms = 0
        self._run_ms = 0
        self._gap_ms = 0

    @property
    def speaking(self) -> bool:
        """Реплика начата и ещё не закрыта."""
        return self._speaking

    @property
    def run_ms(self) -> int:
        """Сколько миллисекунд речь идёт подряд (паузы до 100 мс не считаются разрывом): основа перебивания."""
        return self._run_ms

    @property
    def loud_run_ms(self) -> int:
        """Как `run_ms`, но считаются только кадры громче `loud_db`: живой голос у микрофона громче её эха из колонок."""
        return self._loud_ms

    def push(self, pcm: bytes) -> list[Segment]:
        self._buf.extend(pcm)
        out: list[Segment] = []
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            seg = self._frame(frame)
            if seg is not None:
                out.append(seg)
        return out

    def flush(self) -> Segment | None:
        """Говорящий ушёл или соединение закрыто: закрыть начатую реплику сейчас."""
        if self._buf and self._speaking:
            self._frames.append(bytes(self._buf) + bytes(FRAME_BYTES - len(self._buf)))
        self._buf.clear()
        seg = self._close(forced=True) if self._speaking else None
        self._reset_run()
        return seg

    def _reset_run(self) -> None:
        self._run_ms = self._gap_ms = 0

    def _frame(self, frame: bytes) -> Segment | None:
        voiced = self.vad.is_speech(frame)
        if self.loud_db is not None:
            if voiced and rms_db(frame) >= self.loud_db:
                self._loud_ms += FRAME_MS + self._loud_gap
                self._loud_gap = 0
            else:
                self._loud_gap += FRAME_MS
                if self._loud_gap >= self.RUN_GAP_MS:
                    self._loud_ms = self._loud_gap = 0
        if voiced:
            self._run_ms += FRAME_MS + self._gap_ms
            self._gap_ms = 0
        else:
            self._gap_ms += FRAME_MS
            if self._gap_ms >= self.RUN_GAP_MS:
                self._reset_run()
        if not self._speaking:
            self._preroll.append(frame)
            self._onset = self._onset + 1 if voiced else 0
            if self._onset * FRAME_MS >= self.cfg.onset_ms:
                self._speaking = True
                self._frames = list(self._preroll)
                self._voiced_ms = self._onset * FRAME_MS
                self._silence_ms = 0
                self._preroll.clear()
                self._onset = 0
            return None
        self._frames.append(frame)
        if voiced:
            self._voiced_ms += FRAME_MS
            self._silence_ms = 0
        else:
            self._silence_ms += FRAME_MS
        if self._silence_ms >= self.cfg.tail_ms:
            return self._close()
        if len(self._frames) * FRAME_MS >= self.cfg.max_ms:
            return self._close(forced=True)
        return None

    def _close(self, forced: bool = False) -> Segment | None:
        frames, voiced = self._frames, self._voiced_ms
        extra = max(0, self._silence_ms - self.cfg.keep_tail_ms) // FRAME_MS
        if extra and not forced:
            frames = frames[:len(frames) - extra]
        self._frames, self._speaking, self._voiced_ms, self._silence_ms, self._onset = [], False, 0, 0, 0
        self.vad.reset()
        if voiced < self.cfg.min_speech_ms or not frames:
            return None
        pcm = b"".join(frames)
        return Segment(pcm=pcm, duration_ms=len(pcm) // BYTES_PER_MS, speech_ms=voiced, forced=forced)
