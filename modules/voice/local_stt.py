"""Локальный распознаватель на процессоре (faster-whisper, int8): запасной путь для микрофона и общий для слуха.

Один экземпляр на процесс модуля (`shared()`): микрофон (запасной путь STTClient) и слух системного звука (hearing.py)
делят одну загруженную модель, а не грузят её дважды. Потоков процессора не больше `GITS_STT_THREADS` (по умолчанию 2),
чтобы не душить игру и LM Studio. Модель: `GITS_STT_MODEL` (по умолчанию small).

Веса НЕ скачиваются: модель открывается с `local_files_only=True`. Если веса не лежат в кэше, распознавание просто
недоступно, и состояние честно говорит «модель не загружена». Загрузка из сети только по разрешению владельца (правило
модуля). Проверка повторяется не чаще раза в RETRY_S секунд, чтобы не дёргать диск на каждой реплике.
"""
import logging
import os
import re
import threading
import time

import numpy as np
from envcompat import env

logger = logging.getLogger("local_stt")

SR = 16000
MODEL_NAME = env("GITS_STT_MODEL", "small")
RETRY_S = 60.0                      # модель не нашлась: следующая проверка не раньше, чем через столько секунд
MIN_LOGPROB = -1.0                  # сегменты с худшей уверенностью отбрасываем (как в прежнем слухе)
LANGUAGE = env("GITS_STT_LANGUAGE", os.getenv("WHISPERX_LANGUAGE", "ru"))     # язык задан явно: автоопределение на коротких фразах выдаёт корейский и прочие выдумки
MAX_NO_SPEECH = 0.6

# Типичные «галлюцинации» Whisper на тишине и музыке (общие для микрофона и слуха)
_JUNK = re.compile(r"субтитры|субтитрами|dimatorzok|продолжение следует|спасибо за просмотр|"
                   r"подписывайтесь|редактор субтитров", re.I)


def _threads() -> int:
    try:
        return max(1, int(env("GITS_STT_THREADS", "2")))
    except ValueError:
        return 2


class LocalSTTUnavailable(RuntimeError):
    """Модель не открылась (нет весов в кэше, нет faster-whisper). Текст пойдёт в состояние и журнал."""


def _default_loader(name: str, threads: int):
    from faster_whisper import WhisperModel
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True)


class LocalWhisper:
    def __init__(self, model_name: str = MODEL_NAME, threads: int | None = None, loader=None, clock=time.monotonic):
        """loader(имя, потоки) -> модель с методом transcribe(audio, ...) как у faster-whisper (тесты подменяют)."""
        self.model_name = model_name
        self.threads = threads if threads is not None else _threads()
        self._loader = loader or _default_loader
        self._clock = clock
        self._model = None
        self._lock = threading.RLock()          # одна модель - один разговор в момент времени
        self._state = "не проверена"           # не проверена | загружена | не загружена
        self._error = ""
        self._failed_at = float("-inf")

    @property
    def state(self) -> str:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    def ensure_model(self):
        """Открыть модель (один раз). Блокирующий: звать через asyncio.to_thread. Бросает LocalSTTUnavailable."""
        with self._lock:
            if self._model is not None:
                return self._model
            if self._state == "не загружена" and self._clock() - self._failed_at < RETRY_S:
                raise LocalSTTUnavailable(self._error or "модель не загружена")
            try:
                self._model = self._loader(self.model_name, self.threads)
            except Exception as e:
                self._state, self._error = "не загружена", str(e)[:160] or type(e).__name__
                self._failed_at = self._clock()
                logger.info(f"Модель распознавания {self.model_name} не загружена (скачивание без разрешения не делаю): {self._error}")
                raise LocalSTTUnavailable(self._error) from e
            self._state, self._error = "загружена", ""
            logger.info(f"Модель распознавания {self.model_name} открыта на процессоре ({self.threads} потоков)")
            return self._model

    def check(self) -> str:
        """Проверить, открывается ли модель, без исключений (состояние для дашборда). Блокирующий."""
        try:
            self.ensure_model()
        except LocalSTTUnavailable:
            pass
        return self._state

    def transcribe_pcm(self, pcm: bytes, sample_rate: int = SR) -> str:
        """Сырой PCM int16 моно → текст. Блокирующий."""
        if sample_rate != SR:
            raise ValueError(f"локальное распознавание работает на {SR} Гц, пришло {sample_rate}")
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self.transcribe_array(audio)

    def transcribe_array(self, audio: np.ndarray) -> str:
        """Звук float32 на 16 кГц → текст без сомнительных сегментов и выдумок. Блокирующий."""
        with self._lock:
            model = self.ensure_model()
            segments, _ = model.transcribe(audio, language=LANGUAGE or None, vad_filter=True, beam_size=1, condition_on_previous_text=False)
            out = []
            for s in segments:
                if s.no_speech_prob > MAX_NO_SPEECH or s.avg_logprob < MIN_LOGPROB:
                    continue
                t = s.text.strip()
                if t and not _JUNK.search(t):
                    out.append(t)
            return " ".join(out)


_shared: LocalWhisper | None = None
_shared_lock = threading.Lock()


def shared() -> LocalWhisper:
    """Общий экземпляр на процесс: слух и запасной путь микрофона делят одну модель."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = LocalWhisper()
        return _shared
