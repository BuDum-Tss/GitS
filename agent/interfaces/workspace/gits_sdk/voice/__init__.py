"""Голосовые адаптеры: клиент движка голоса ядра, проводной протокол и перевод звука (docs/VOICE_ENGINE.md).

Нужен numpy (`pip install gits-sdk[voice]`); остальное только aiohttp."""
from . import protocol
from .audio import Resampler, resample, to_16k_mono, to_16k_mono_stream, to_48k_stereo, to_48k_stereo_stream
from .client import PlayStream, VoiceAdapterClient, default_url
from .protocol import FRAME_BYTES, FRAME_MS, SAMPLE_RATE, VOICE_PROTOCOL, WS_PATH, VoiceProtocolError

__all__ = [
    "FRAME_BYTES", "FRAME_MS", "PlayStream", "Resampler", "SAMPLE_RATE", "VOICE_PROTOCOL", "VoiceAdapterClient",
    "VoiceProtocolError", "WS_PATH", "default_url", "protocol", "resample", "to_16k_mono", "to_16k_mono_stream",
    "to_48k_stereo", "to_48k_stereo_stream",
]
