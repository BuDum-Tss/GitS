"""Настройки сервиса распознавания: из переменных окружения контейнера, с безопасными значениями по умолчанию.

Имена GITS_STT_* те же, что у распознавателя голоса ПК (services/voice/local_stt.py), поэтому прежний .env подходит и здесь.
Служебные (воркеры, очередь, таймаут, порт) без префикса GITS: STT_*.
"""
import os
from dataclasses import dataclass, fields
from typing import Mapping

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2                       # s16le, моно
MIN_AUDIO_S = 0.2                          # короче: 400
MAX_AUDIO_S = 60.0                         # длиннее: 413
MAX_BODY_BYTES = int(MAX_AUDIO_S * SAMPLE_RATE * BYTES_PER_SAMPLE)
MIN_BODY_BYTES = int(MIN_AUDIO_S * SAMPLE_RATE * BYTES_PER_SAMPLE)

ENGINES = ("gigaam", "whisper")


def _int(environ: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(environ.get(name, "") or default).strip())
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _float(environ: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(str(environ.get(name, "") or default).strip())
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _str(environ: Mapping[str, str], name: str, default: str) -> str:
    return str(environ.get(name, "") or "").strip() or default


@dataclass(frozen=True)
class SttConfig:
    host: str = "127.0.0.1"                # в контейнере образ задаёт 0.0.0.0
    port: int = 8772
    engine: str = "gigaam"                 # основной движок: gigaam | whisper (whisper сразу без GigaAM)
    gigaam_model: str = "gigaam-v3-e2e-rnnt"
    gigaam_repo: str = "istupakov/gigaam-v3-onnx"      # откуда onnx-asr берёт веса (кэш Hugging Face)
    whisper_model: str = "small"           # имя из faster-whisper (репозиторий Systran/faster-whisper-<имя>) или полное имя репозитория
    language: str = "ru"                   # язык по умолчанию, если запрос его не указал
    threads: int = 2                       # потоков процессора на одно распознавание
    workers: int = 2                       # распознаваний одновременно
    queue: int = 8                         # сколько запросов ждёт своей очереди сверх workers; больше: 503
    timeout_s: float = 30.0                # на весь запрос вместе с ожиданием в очереди; дольше: 504
    retry_s: float = 60.0                  # модель не открылась: следующая попытка через столько секунд (веса могли появиться)
    warmup: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "SttConfig":
        e = os.environ if environ is None else environ
        engine = _str(e, "GITS_STT_ENGINE", "gigaam").lower()
        return cls(
            host=_str(e, "STT_HOST", cls.host), port=_int(e, "STT_PORT", cls.port, 1, 65535),
            engine=engine if engine in ENGINES else "gigaam",
            gigaam_model=_str(e, "GITS_STT_GIGAAM", cls.gigaam_model),
            gigaam_repo=_str(e, "GITS_STT_GIGAAM_REPO", cls.gigaam_repo),
            whisper_model=_str(e, "GITS_STT_MODEL", cls.whisper_model),
            language=_str(e, "GITS_STT_LANGUAGE", cls.language).lower(),
            threads=_int(e, "GITS_STT_THREADS", cls.threads, 1, 64),
            workers=_int(e, "STT_WORKERS", cls.workers, 1, 16),
            queue=_int(e, "STT_QUEUE", cls.queue, 0, 256),
            timeout_s=_float(e, "STT_TIMEOUT_S", cls.timeout_s, 1.0, 300.0),
            retry_s=_float(e, "STT_RETRY_S", cls.retry_s, 1.0, 3600.0),
            warmup=_str(e, "STT_WARMUP", "1").lower() not in ("0", "false", "no", "off"),
        )

    def public(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}
