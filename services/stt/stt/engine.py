"""Движки распознавания: общий интерфейс `Engine`, GigaAM v3 (основной), faster-whisper (запасной) и `FakeEngine` для тестов.

Логика перенесена из services/voice/local_stt.py (ДУБЛИРОВАНИЕ: фильтр мусорных фраз, пороги Whisper, поиск весов GigaAM в кэше).
Пакеты независимы, поэтому копия, а не импорт; когда голос ПК перейдёт на этот сервис, копия в services/voice/local_stt.py уйдёт.

Что не меняется относительно оригинала:
- веса НЕ скачиваются: всё открывается из кэша Hugging Face (`HF_HOME`), без сети; нет весов: `EngineUnavailable`;
- GigaAM (onnx-asr, int8) только русский; если он не открылся, `FallbackEngine` берёт faster-whisper (int8);
- потоков процессора не больше `GITS_STT_THREADS`.

Все методы распознавания блокирующие: сервер зовёт их из пула потоков.
"""
import logging
import os
import re
import threading
import time
from typing import Callable

import numpy as np

from .config import SAMPLE_RATE, SttConfig

logger = logging.getLogger("stt.engine")

MIN_LOGPROB = -1.0                  # сегменты Whisper с худшей уверенностью отбрасываем
MAX_NO_SPEECH = 0.6

# Типичные «галлюцинации» на тишине и музыке (копия _JUNK из services/voice/local_stt.py)
_JUNK = re.compile(r"субтитры|субтитрами|dimatorzok|продолжение следует|спасибо за просмотр|"
                   r"подписывайтесь|редактор субтитров", re.I)


class EngineUnavailable(RuntimeError):
    """Модель не открылась (нет весов в кэше, нет пакета). Текст идёт в /health и журнал."""


def is_junk(text: str) -> bool:
    return bool(_JUNK.search(text))


def pcm_to_float(pcm: bytes) -> np.ndarray:
    """Сырой PCM s16le моно -> float32 в диапазоне [-1, 1). Нечётный хвост (полсэмпла) отбрасывается."""
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


class Engine:
    """Интерфейс. `load()` и `transcribe()` блокирующие; `load()` можно звать повторно (после неудачи)."""

    name = "engine"
    model = ""
    languages: frozenset[str] | None = None        # None: любой язык

    @property
    def loaded(self) -> bool:
        raise NotImplementedError

    def load(self) -> None:
        """Открыть модель. Бросает EngineUnavailable."""
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray, lang: str) -> str:
        """Звук float32 на 16 кГц -> текст без сомнительных сегментов и выдумок."""
        raise NotImplementedError

    def supports(self, lang: str) -> bool:
        return self.languages is None or lang in self.languages


# ---------------------------------------------------------------------------------------------------- GigaAM

def gigaam_encoder_name(model: str) -> str:
    """Файл энкодера в репозитории onnx-asr для модели: gigaam-v3-e2e-rnnt -> v3_e2e_rnnt_encoder.int8.onnx."""
    return model.removeprefix("gigaam-").replace("-", "_") + "_encoder.int8.onnx"


def _gigaam_loader(model: str, repo: str, threads: int):
    """Открыть GigaAM из кэша Hugging Face (скачивания нет). Нет пакета или весов: исключение."""
    import onnx_asr
    import onnxruntime as ort
    from huggingface_hub import try_to_load_from_cache

    encoder = try_to_load_from_cache(repo, gigaam_encoder_name(model))       # путь к файлу в снимке кэша или None
    if not isinstance(encoder, str):
        raise FileNotFoundError(f"весов {repo} ({gigaam_encoder_name(model)}) нет в кэше Hugging Face ({os.environ.get('HF_HOME', '')})")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    return onnx_asr.load_model(model, os.path.dirname(encoder), quantization="int8", sess_options=opts,
                               providers=["CPUExecutionProvider"])


class GigaAmEngine(Engine):
    name = "gigaam"
    languages = frozenset({"ru"})

    def __init__(self, config: SttConfig, loader: Callable | None = None):
        self.model = config.gigaam_model
        self._repo, self._threads = config.gigaam_repo, config.threads
        self._loader = loader or _gigaam_loader
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                self._model = self._loader(self.model, self._repo, self._threads)
            except Exception as e:
                raise EngineUnavailable(str(e)[:200] or type(e).__name__) from e
            logger.info("GigaAM %s открыт на процессоре (%d потоков)", self.model, self._threads)

    def transcribe(self, audio: np.ndarray, lang: str) -> str:
        if self._model is None:
            raise EngineUnavailable("модель не загружена")
        text = str(self._model.recognize(audio.astype(np.float32), sample_rate=SAMPLE_RATE) or "").strip()
        return "" if is_junk(text) else text


# ---------------------------------------------------------------------------------------------------- Whisper

def _whisper_loader(model: str, threads: int):
    from faster_whisper import WhisperModel
    return WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads, local_files_only=True)


class WhisperEngine(Engine):
    name = "whisper"
    languages = None

    def __init__(self, config: SttConfig, loader: Callable | None = None):
        self.model = config.whisper_model
        self._threads = config.threads
        self._loader = loader or _whisper_loader
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                self._model = self._loader(self.model, self._threads)
            except Exception as e:
                raise EngineUnavailable(str(e)[:200] or type(e).__name__) from e
            logger.info("faster-whisper %s открыт на процессоре (%d потоков)", self.model, self._threads)

    def transcribe(self, audio: np.ndarray, lang: str) -> str:
        if self._model is None:
            raise EngineUnavailable("модель не загружена")
        # язык задан явно: автоопределение на коротких фразах выдаёт корейский и прочие выдумки
        segments, _ = self._model.transcribe(audio, language=lang or None, vad_filter=True, beam_size=1,
                                             condition_on_previous_text=False)
        out = []
        for s in segments:
            if s.no_speech_prob > MAX_NO_SPEECH or s.avg_logprob < MIN_LOGPROB:
                continue
            t = s.text.strip()
            if t and not is_junk(t):
                out.append(t)
        return " ".join(out)


# ---------------------------------------------------------------------------------------------------- составной

class FallbackEngine(Engine):
    """Основной движок, а если он не открылся, запасной. Имя и модель берутся у того, что реально работает."""

    def __init__(self, primary: Engine, fallback: Engine):
        self.primary, self.fallback = primary, fallback
        self._active: Engine | None = None

    @property
    def active(self) -> Engine:
        return self._active or self.primary

    @property
    def name(self) -> str:                          # type: ignore[override]
        return self.active.name

    @property
    def model(self) -> str:                         # type: ignore[override]
        return self.active.model

    @property
    def languages(self) -> frozenset[str] | None:   # type: ignore[override]
        return self.active.languages

    @property
    def loaded(self) -> bool:
        return self._active is not None and self._active.loaded

    def load(self) -> None:
        if self._active is not None and self._active.loaded:
            return
        try:
            self.primary.load()
            self._active = self.primary
            return
        except EngineUnavailable as e:
            logger.info("%s не открылся (%s), пробую %s", self.primary.name, e, self.fallback.name)
            first = str(e)
        try:
            self.fallback.load()
        except EngineUnavailable as e:
            raise EngineUnavailable(f"{self.primary.name}: {first}; {self.fallback.name}: {e}") from e
        self._active = self.fallback

    def transcribe(self, audio: np.ndarray, lang: str) -> str:
        return self.active.transcribe(audio, lang)


def build_engine(config: SttConfig, *, gigaam_loader: Callable | None = None, whisper_loader: Callable | None = None) -> Engine:
    """Выбор движка по `GITS_STT_ENGINE`: gigaam (с запасным whisper) или сразу whisper."""
    whisper = WhisperEngine(config, whisper_loader)
    if config.engine == "whisper":
        return whisper
    return FallbackEngine(GigaAmEngine(config, gigaam_loader), whisper)


# ---------------------------------------------------------------------------------------------------- для тестов

class FakeEngine(Engine):
    """Движок без модели: отдаёт заготовленный текст. Для тестов API и совместимости с клиентом ядра."""

    name = "fake"
    model = "fake"

    def __init__(self, text: str = "привет", *, delay_s: float = 0.0, fail_load: str = "", fail: Exception | None = None,
                 languages: frozenset[str] | None = None, gate: threading.Event | None = None):
        self.text, self.delay_s, self.fail_load, self.fail = text, delay_s, fail_load, fail
        self.languages = languages
        self.gate = gate                      # если задан: transcribe ждёт его, пока тест не разрешит
        self.calls: list[tuple[int, str]] = []
        self.active_now = 0
        self.max_active = 0
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        if self.fail_load:
            raise EngineUnavailable(self.fail_load)
        self._loaded = True

    def transcribe(self, audio: np.ndarray, lang: str) -> str:
        with self._lock:
            self.calls.append((len(audio), lang))
            self.active_now += 1
            self.max_active = max(self.max_active, self.active_now)
        try:
            if self.gate is not None:
                self.gate.wait(10)
            if self.delay_s:
                time.sleep(self.delay_s)
            if self.fail is not None:
                raise self.fail
            return self.text
        finally:
            with self._lock:
                self.active_now -= 1
