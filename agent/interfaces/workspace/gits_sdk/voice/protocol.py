"""Проводной протокол голосового адаптера и движка голоса (docs/VOICE_ENGINE.md, раздел «Протокол»).

Один WebSocket `/voice/ws` на сессию адаптера. Два вида кадров:

  - текстовые JSON-сообщения управления `{"type": "...", ...}` (hello, welcome, speaker.join, play.start, ...);
  - бинарные кадры звука: заголовок 12 байт (`HEADER`) и тело PCM s16le, 16000 Гц, моно.

Этот модуль общий для движка (ядро) и адаптеров (SDK): он ничего не знает о сети, только кодирует, декодирует и проверяет.
Нарушение формата это `VoiceProtocolError`; получатель отвечает сообщением `error` и не доверяет остальному потоку.
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from typing import Any

VOICE_PROTOCOL = 1                      # версия протокола; меняется только несовместимо
WS_PATH = "/voice/ws"                   # адрес входа на HTTP-сервере ядра (порт дашборда)

# формат звука: один на весь протокол, адаптер приводит свой звук к нему (`audio.py`)
CODEC = "pcm_s16le"
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2                        # байт на отсчёт
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000          # 320 отсчётов
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH * CHANNELS   # 640 байт

# лимиты (проверяются при разборе): чужое сообщение не должно раздувать память движка
MAX_CONTROL_BYTES = 64 * 1024           # текстовое сообщение управления
MAX_PCM_BYTES = FRAME_BYTES * 10        # тело одного бинарного кадра: до 200 мс (движок сам режет на 20 мс)
MAX_WS_MESSAGE_BYTES = MAX_CONTROL_BYTES
MAX_ID_LEN = 128
MAX_NAME_LEN = 80
MAX_TITLE_LEN = 120                     # человекочитаемое название канала в hello (необязательно)
MAX_TEXT_LEN = 4000                     # текст реплики в play.start

# бинарный кадр: тип, флаги (0), говорящий, поток, номер
HEADER = struct.Struct("!BBHII")
HEADER_SIZE = HEADER.size               # 12
KIND_MIC = 1                            # адаптер -> движок: звук говорящего (speaker = idx из speaker.join, stream = 0)
KIND_PLAY = 2                           # движок -> адаптер: звук реплики (speaker = 0, stream = play_id из play.start)
MAX_SPEAKER_IDX = 0xFFFF

# сообщения управления
TO_ENGINE = frozenset({"hello", "speaker.join", "speaker.leave", "speaking.start", "speaking.stop", "play.ack", "bye"})
TO_ADAPTER = frozenset({"welcome", "play.start", "play.end", "play.stop", "leave", "error", "bye"})

PLAY_REASONS = ("completed", "stop", "replaced", "barge_in", "closed", "error")   # reason в play.ack и ended

_ID_RE = re.compile(r"^[\w.:@\-]+$")
_ADAPTER_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")


class VoiceProtocolError(ValueError):
    """Сообщение или кадр нарушают протокол; текст объясняет, что именно."""


@dataclass(frozen=True)
class WireFrame:
    """Разобранный бинарный кадр."""
    kind: int
    speaker: int
    stream: int
    seq: int
    pcm: bytes


def audio_format() -> dict:
    """Описание формата звука для hello и welcome."""
    return {"codec": CODEC, "rate": SAMPLE_RATE, "channels": CHANNELS, "frame_ms": FRAME_MS}


# ── бинарные кадры ────────────────────────────────────────────────────────────

def pack_frame(kind: int, speaker: int, stream: int, seq: int, pcm: bytes) -> bytes:
    """Заголовок и тело кадра. Те же проверки, что при разборе: нарушение ловится у отправителя."""
    if kind not in (KIND_MIC, KIND_PLAY):
        raise VoiceProtocolError(f"неизвестный тип кадра {kind}")
    if not 0 <= speaker <= MAX_SPEAKER_IDX:
        raise VoiceProtocolError("номер говорящего вне 0..65535")
    _check_pcm(pcm)
    return HEADER.pack(kind, 0, speaker, stream & 0xFFFFFFFF, seq & 0xFFFFFFFF) + pcm


def unpack_frame(data: bytes) -> WireFrame:
    """Разобрать бинарный кадр; мусор, чужой тип, чужие флаги и неверный размер тела это VoiceProtocolError."""
    if len(data) <= HEADER_SIZE:
        raise VoiceProtocolError("бинарный кадр короче заголовка или без звука")
    kind, flags, speaker, stream, seq = HEADER.unpack_from(data)
    if kind not in (KIND_MIC, KIND_PLAY):
        raise VoiceProtocolError(f"неизвестный тип кадра {kind}")
    if flags != 0:
        raise VoiceProtocolError("флаги кадра зарезервированы и должны быть 0")
    pcm = bytes(data[HEADER_SIZE:])
    _check_pcm(pcm)
    return WireFrame(kind, speaker, stream, seq, pcm)


def _check_pcm(pcm: bytes) -> None:
    if not pcm:
        raise VoiceProtocolError("в кадре нет звука")
    if len(pcm) > MAX_PCM_BYTES:
        raise VoiceProtocolError(f"звук в кадре больше {MAX_PCM_BYTES} байт")
    if len(pcm) % SAMPLE_WIDTH:
        raise VoiceProtocolError("длина звука не кратна размеру отсчёта (2 байта)")


# ── сообщения управления ──────────────────────────────────────────────────────

def encode(msg_type: str, **fields: Any) -> str:
    """JSON-текст сообщения. Поля со значением None не пишутся."""
    body = {"type": msg_type, **{k: v for k, v in fields.items() if v is not None}}
    text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise VoiceProtocolError(f"сообщение «{msg_type}» больше {MAX_CONTROL_BYTES} байт")
    return text


def decode(text: str, *, allowed: frozenset[str]) -> dict:
    """Разобрать и проверить сообщение; `allowed` это допустимые типы для направления (TO_ENGINE или TO_ADAPTER)."""
    if not isinstance(text, str):
        raise VoiceProtocolError("сообщение управления должно быть текстом")
    if len(text.encode("utf-8", errors="replace")) > MAX_CONTROL_BYTES:
        raise VoiceProtocolError(f"сообщение больше {MAX_CONTROL_BYTES} байт")
    try:
        msg = json.loads(text)
    except ValueError as e:
        raise VoiceProtocolError(f"не JSON: {e}") from None
    if not isinstance(msg, dict):
        raise VoiceProtocolError("сообщение должно быть объектом")
    kind = msg.get("type")
    if not isinstance(kind, str) or kind not in allowed:
        raise VoiceProtocolError(f"неизвестный или недопустимый тип сообщения: {str(kind)[:40]!r}")
    return _VALIDATORS[kind](msg)


def _str(msg: dict, key: str, *, limit: int = MAX_ID_LEN, pattern: re.Pattern | None = None, required: bool = True) -> str | None:
    value = msg.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise VoiceProtocolError(f"поле «{key}» должно быть непустой строкой не длиннее {limit}")
    if pattern is not None and not pattern.match(value):
        raise VoiceProtocolError(f"поле «{key}» содержит недопустимые символы")
    return value


def _int(msg: dict, key: str, lo: int, hi: int, *, required: bool = True) -> int | None:
    value = msg.get(key)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise VoiceProtocolError(f"поле «{key}» должно быть целым от {lo} до {hi}")
    return value


def _bool(msg: dict, key: str, default: bool = False) -> bool:
    value = msg.get(key, default)
    if not isinstance(value, bool):
        raise VoiceProtocolError(f"поле «{key}» должно быть true или false")
    return value


def _hello(msg: dict) -> dict:
    if msg.get("version") != VOICE_PROTOCOL:
        raise VoiceProtocolError(f"версия протокола {msg.get('version')!r} не поддерживается (нужна {VOICE_PROTOCOL})")
    _str(msg, "adapter", pattern=_ADAPTER_RE)
    _str(msg, "session", pattern=_ID_RE)
    _str(msg, "title", limit=MAX_TITLE_LEN, required=False)
    caps = msg.get("capabilities", {})
    if not isinstance(caps, dict):
        raise VoiceProtocolError("capabilities должно быть объектом")
    clean = {}
    for name in ("echo_free", "can_barge_in", "speakers_labelled", "direct"):
        clean[name] = _bool(caps, name, default=(name == "speakers_labelled"))
    fmt = msg.get("format")
    if fmt != audio_format():
        raise VoiceProtocolError(f"формат звука должен быть {audio_format()}")
    return {**msg, "capabilities": clean}


def _speaker(msg: dict, join: bool) -> dict:
    _str(msg, "id", pattern=_ID_RE)
    if join:
        _str(msg, "name", limit=MAX_NAME_LEN, required=False)
        _int(msg, "idx", 1, MAX_SPEAKER_IDX)
        _bool(msg, "is_owner")
        _bool(msg, "is_bot")
    return msg


def _play_ack(msg: dict) -> dict:
    _str(msg, "utterance_id", pattern=_ID_RE)
    _int(msg, "spoken_upto", 0, 24 * 3600 * 1000)
    if msg.get("reason") not in PLAY_REASONS:
        raise VoiceProtocolError(f"reason должен быть одним из {PLAY_REASONS}")
    return msg


def _welcome(msg: dict) -> dict:
    _int(msg, "version", VOICE_PROTOCOL, VOICE_PROTOCOL)
    _str(msg, "session", pattern=_ID_RE)
    return msg


def _play_start(msg: dict) -> dict:
    _str(msg, "utterance_id", pattern=_ID_RE)
    _int(msg, "play_id", 1, 0xFFFFFFFF)
    if not isinstance(msg.get("text", ""), str) or len(msg.get("text", "")) > MAX_TEXT_LEN:
        raise VoiceProtocolError("text должен быть строкой")
    _int(msg, "total_ms", 0, 24 * 3600 * 1000)
    return msg


def _play_end(msg: dict) -> dict:
    _str(msg, "utterance_id", pattern=_ID_RE)
    _int(msg, "play_id", 1, 0xFFFFFFFF)
    return msg


def _play_stop(msg: dict) -> dict:
    _str(msg, "utterance_id", pattern=_ID_RE, required=False)
    if msg.get("reason") not in PLAY_REASONS:
        raise VoiceProtocolError(f"reason должен быть одним из {PLAY_REASONS}")
    return msg


def _error(msg: dict) -> dict:
    _str(msg, "code", limit=64)
    if not isinstance(msg.get("message", ""), str):
        raise VoiceProtocolError("message должен быть строкой")
    return msg


def _any(msg: dict) -> dict:
    return msg


_VALIDATORS = {
    "hello": _hello,
    "welcome": _welcome,
    "speaker.join": lambda m: _speaker(m, True),
    "speaker.leave": lambda m: _speaker(m, False),
    "speaking.start": lambda m: _speaker(m, False),
    "speaking.stop": lambda m: _speaker(m, False),
    "play.ack": _play_ack,
    "play.start": _play_start,
    "play.end": _play_end,
    "play.stop": _play_stop,
    "leave": _any,
    "error": _error,
    "bye": _any,
}
