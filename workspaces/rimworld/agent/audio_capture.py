"""Запись звука компьютера (то, что идёт в колонки: голос бота и игра) в WAV через WASAPI loopback.

Нужна, чтобы в видео забега был слышен голос бота. Работает в потоке; без библиотеки SoundCard или без устройства
запись просто не включается (`ok` остаётся False), видео пишется без звука.

Запись идёт кусками (`raw_HHMMSS_a01.wav`, `_a02`…) не длиннее PART_SECONDS: файл не растёт без предела, а сбой портит
только текущий кусок. У каждого куска есть точное время начала (`AudioPart.started_at`, unix); склейка в `run_recorder`
идёт по этому времени. Если поток потерял кадры (WASAPI сообщает «data discontinuity»), недостающее время заполняется
тишиной, а при долгом провале начинается новый кусок.

Сбой потока (например, `RuntimeError: Error 0x100000001`: soundcard считает ошибкой код S_FALSE, который WASAPI отдаёт при
смене устройства вывода) завершает поток: `ok` становится False, `error` объясняет причину. Перезапускает запись звука
`RunRecorder.ensure_audio` (новый `LoopbackRecorder`, следующий номер куска).
"""
import json
import logging
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("audio_capture")

RATE = 44100
CHANNELS = 2
CHUNK = 2048
PART_SECONDS = 1800         # длина одного куска (REC-1): 30 минут, около 317 МБ
PAD_MIN_S = 0.5             # потеря кадров больше этого заполняется тишиной в том же куске
SPLIT_GAP_S = 5.0           # потеря больше этого: новый кусок со своим временем начала
VOICE_PEAK = 0.02           # пик громче этого считаем звуком, а не тишиной


@dataclass
class AudioPart:
    path: Path
    started_at: float       # когда пошёл первый кадр куска (unix)
    frames: int = 0

    @property
    def seconds(self) -> float:
        return self.frames / RATE


def part_path(base: Path, index: int) -> Path:
    """`raw_000912.wav` -> `raw_000912_a01.wav`."""
    return base.with_name(f"{base.stem}_a{index:02d}{base.suffix or '.wav'}")


def _default_source():
    """Устройство вывода по умолчанию в режиме loopback (выбирается заново при каждом перезапуске: оно могло смениться)."""
    import soundcard as sc
    speaker = sc.default_speaker()
    mic = sc.get_microphone(speaker.name, include_loopback=True)
    return mic.recorder(samplerate=RATE, channels=CHANNELS, blocksize=CHUNK)


class LoopbackRecorder:
    """Один сеанс записи звука: поток, который пишет подряд идущие куски. Умерший сеанс заменяется новым."""

    def __init__(self, base: Path, part_seconds: float = PART_SECONDS, first_index: int = 1, source=None,
                 wall=time.time, mono=time.monotonic):
        self.base = Path(base)
        self.part_seconds = part_seconds
        self.parts: list[AudioPart] = []
        self.peak = 0.0             # самый громкий отсчёт за всю запись, 0..1
        self.recent_peak = 0.0      # пик за последние секунды: для проверки «звук идёт прямо сейчас»
        self.error = ""
        self._index = first_index
        self._source = source or _default_source      # () -> контекст, дающий объект с record(numframes)
        self._wall, self._mono = wall, mono
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._wav = None
        self._t0_wall = 0.0
        self._t0_mono = 0.0
        self._frames_total = 0

    @property
    def ok(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self.error)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def started_at(self) -> float:
        """Время начала первого куска (unix); 0, если запись не началась."""
        return self.parts[0].started_at if self.parts else 0.0

    @property
    def next_index(self) -> int:
        return self._index

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="audio-loopback", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.ok

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _open_part(self, started_at: float):
        part = AudioPart(part_path(self.base, self._index), started_at)
        self._index += 1
        wav = wave.open(str(part.path), "wb")
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        self._wav = wav
        self.parts.append(part)
        self._save_meta(part)

    def _close_part(self):
        if self._wav is not None:
            try:
                self._wav.close()
            finally:
                self._wav = None
                self._save_meta(self.parts[-1])

    @staticmethod
    def _save_meta(part: AudioPart):
        """Рядом с куском лежит `<кусок>.json` с точным временем начала: по нему запись собирают заново, если бот
        остановился посреди забега (`tools/produce_run.py`)."""
        try:
            part.path.with_suffix(".json").write_text(
                json.dumps({"started_at": part.started_at, "frames": part.frames, "rate": RATE}), encoding="utf-8")
        except OSError as e:
            logger.warning(f"Не записать время начала куска звука: {e}")

    def _write(self, pcm: bytes):
        self._wav.writeframes(pcm)
        self.parts[-1].frames += len(pcm) // (2 * CHANNELS)
        self._frames_total += len(pcm) // (2 * CHANNELS)

    def _elapsed(self) -> float:
        return self._mono() - self._t0_mono

    def _run(self):
        try:
            import numpy as np
            ctx = self._source()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:200]
            logger.warning(f"Запись звука недоступна: {self.error}")
            self._ready.set()
            return
        try:
            with ctx as rec:
                self._t0_wall, self._t0_mono = self._wall(), self._mono()
                self._open_part(self._t0_wall)
                self._ready.set()
                window = 0.0
                part_frames = int(self.part_seconds * RATE)
                while not self._stop.is_set():
                    data = rec.record(numframes=CHUNK)
                    n = int(data.shape[0])
                    peak = float(np.abs(data).max()) if data.size else 0.0
                    self.peak = max(self.peak, peak)
                    window = peak if peak > window * 0.97 else window * 0.97     # медленно затухающий пик
                    self.recent_peak = window
                    lag = self._elapsed() - (self._frames_total + n) / RATE      # сколько времени потеряно перед блоком
                    if lag > SPLIT_GAP_S:
                        self._close_part()
                        self._open_part(self._t0_wall + self._elapsed() - n / RATE)
                        self._frames_total = int((self._elapsed() - n / RATE) * RATE)
                    elif lag > PAD_MIN_S:
                        self._write(b"\x00" * (int(lag * RATE) * CHANNELS * 2))
                    self._write((np.clip(data, -1, 1) * 32767).astype("<i2").tobytes())
                    if self.parts[-1].frames >= part_frames:
                        self._close_part()
                        self._open_part(self._t0_wall + self._elapsed())
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"[:200]
            logger.warning(f"Запись звука оборвалась: {self.error}")
        finally:
            self._close_part()
            self._ready.set()

    def has_sound(self) -> bool:
        return self.peak >= VOICE_PEAK
