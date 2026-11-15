"""Проводной протокол адаптер <-> движок: описание и код общие с SDK (`gits_sdk/voice/protocol.py`), здесь только вход для движка.

Формат кадров, сообщения (hello, welcome, speaker.join, play.start, play.ack ...), лимиты и проверки: docs/VOICE_ENGINE.md,
раздел «Протокол»."""
from gits_sdk.voice.protocol import *  # noqa: F401,F403
from gits_sdk.voice.protocol import (  # noqa: F401
    HEADER_SIZE,
    TO_ADAPTER,
    TO_ENGINE,
    VOICE_PROTOCOL,
    WS_PATH,
    VoiceProtocolError,
    WireFrame,
    audio_format,
    decode,
    encode,
    pack_frame,
    unpack_frame,
)
