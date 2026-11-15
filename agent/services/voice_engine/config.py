"""Настройки движка голоса: из переменных окружения ядра (`.env`), с безопасными значениями по умолчанию.

Движок включён, только если задано, откуда брать модели: адреса распознавания и синтеза. Явное `GITS_VOICE_ENGINE=0` выключает
его при любых адресах, `GITS_VOICE_ENGINE=1` включает и требует адресов (без них движок остаётся выключенным, причина в
`disabled_reason`). Секретов здесь нет: токен шины ядра движок берёт у самой шины.
"""
from dataclasses import dataclass, fields
from typing import Mapping

_TRUE, _FALSE = {"1", "true", "yes", "on", "да"}, {"0", "false", "no", "off", "нет"}

BARGE_POLICIES = ("off", "any_speaker")


def _first(environ: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = str(environ.get(name, "") or "").strip()
        if value:
            return value
    return default


def _num(environ: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(environ.get(name, "") or default)
    except ValueError:
        return default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class EngineConfig:
    enabled: bool = False
    disabled_reason: str = "адреса распознавания и синтеза не заданы"
    stt_url: str = ""
    tts_url: str = ""
    lang: str = "ru"
    stt_timeout_s: float = 15.0
    tts_timeout_s: float = 60.0
    # сессии и лимиты
    max_sessions: int = 4
    max_speakers: int = 32
    stt_concurrency: int = 2
    stt_pending_per_session: int = 4
    # детектор речи
    vad: str = "energy"                  # energy | silero
    energy_threshold_db: float = -42.0
    silero_model: str = ""
    silero_threshold: float = 0.5
    onset_ms: int = 60
    min_speech_ms: int = 240
    tail_ms: int = 600
    max_utterance_ms: int = 30000
    # перебивание: auto это any_speaker для адаптера с echo_free и can_barge_in, иначе off
    barge_in: str = "auto"
    barge_in_ms: int = 2000
    # воспроизведение
    lead_ms: int = 60
    max_waiting: int = 3
    # голос персонажа (референс и скорость идут контейнеру синтеза)
    tts_ref_audio: str = ""
    tts_ref_text: str = ""
    tts_speed: float = 1.0
    tts_voice: str = ""
    tts_emotion: str = ""
    # конвейерный синтез: деление реплики на предложения и предзагрузка
    tts_split: str = "sentences"         # sentences | off
    tts_prefetch: int = 1
    split_min_chars: int = 25
    split_max_chars: int = 220

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "EngineConfig":
        stt = _first(environ, "GITS_STT_URL", "STT_URL")
        tts = _first(environ, "GITS_TTS_URL", "TTS_URL", "ESPEECH_URL")
        switch = _first(environ, "GITS_VOICE_ENGINE").lower()
        if switch in _FALSE:
            enabled, reason = False, "выключен переменной GITS_VOICE_ENGINE"
        elif not (stt and tts):
            enabled, reason = False, "адреса распознавания и синтеза не заданы (GITS_STT_URL, GITS_TTS_URL)"
        else:
            enabled, reason = True, ""
        barge = _first(environ, "GITS_VOICE_BARGE_IN", default="auto").lower()
        vad = _first(environ, "GITS_VOICE_VAD", default="energy").lower()
        return cls(
            enabled=enabled, disabled_reason=reason, stt_url=stt, tts_url=tts,
            lang=_first(environ, "GITS_VOICE_LANG", default="ru"),
            stt_timeout_s=_num(environ, "GITS_STT_TIMEOUT_S", 15.0, 1.0, 120.0),
            tts_timeout_s=_num(environ, "GITS_TTS_TIMEOUT_S", 60.0, 1.0, 300.0),
            max_sessions=int(_num(environ, "GITS_VOICE_MAX_SESSIONS", 4, 1, 32)),
            max_speakers=int(_num(environ, "GITS_VOICE_MAX_SPEAKERS", 32, 1, 256)),
            vad=vad if vad in ("energy", "silero") else "energy",
            energy_threshold_db=_num(environ, "GITS_VOICE_THRESHOLD_DB", -42.0, -80.0, -10.0),
            silero_model=_first(environ, "GITS_VOICE_SILERO_MODEL"),
            tail_ms=int(_num(environ, "GITS_VOICE_TAIL_MS", 600, 200, 3000)),
            min_speech_ms=int(_num(environ, "GITS_VOICE_MIN_SPEECH_MS", 240, 60, 2000)),
            barge_in=barge if barge in ("auto", *BARGE_POLICIES) else "auto",
            barge_in_ms=int(_num(environ, "GITS_VOICE_BARGE_IN_MS", 2000, 100, 3000)),
            tts_ref_audio=_first(environ, "GITS_TTS_REF_AUDIO"),
            tts_ref_text=_first(environ, "GITS_TTS_REF_TEXT"),
            tts_speed=_num(environ, "GITS_TTS_SPEED", 1.0, 0.5, 1.5),
            tts_voice=_first(environ, "GITS_TTS_VOICE"),
            tts_emotion=_first(environ, "GITS_TTS_EMOTION"),
            tts_split="off" if _first(environ, "GITS_TTS_SPLIT", "TTS_SPLIT", default="sentences").lower() == "off" else "sentences",
            tts_prefetch=int(_num(environ, "GITS_TTS_PREFETCH", _num(environ, "TTS_PREFETCH", 1, 0, 3), 0, 3)),
            split_min_chars=int(_num(environ, "GITS_TTS_SPLIT_MIN", 25, 1, 200)),
            split_max_chars=int(_num(environ, "GITS_TTS_SPLIT_MAX", 220, 40, 600)),
        )

    def public(self) -> dict:
        """Настройки для дашборда: адреса сервисов без вычислений, секретов в конфиге нет."""
        return {f.name: getattr(self, f.name) for f in fields(self)}
