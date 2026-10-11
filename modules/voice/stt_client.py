"""STT-клиент: аудио → WhisperX-сервер (docker/whisperx) → текст, метка говорящего и голосовой эмбеддинг.

Перенесено из GitS (src/audio/stt_client.py), без pydantic-настроек: адрес берётся из WHISPERX_URL
(по умолчанию http://127.0.0.1:9000). Сервер OpenAI-совместим (/v1/audio/transcriptions) и добавляет диаризацию.
"""
import io
import os
import wave
from dataclasses import dataclass

import httpx

SAMPLE_RATE = 16_000
URL = os.getenv("WHISPERX_URL", "http://127.0.0.1:9000")
LANGUAGE = os.getenv("WHISPERX_LANGUAGE", "ru")


def pcm_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Сырой PCM (int16, моно) в WAV-контейнер."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@dataclass
class Transcript:
    text: str
    # метка диаризации (SPEAKER_00): НЕ стабильна между вызовами, WhisperX диаризует каждый фрагмент отдельно
    speaker: str | None = None
    # голосовой эмбеддинг доминирующего говорящего (стабилен между вызовами): по нему voice_id узнаёт человека
    speaker_embedding: list[float] | None = None


def dominant_speaker(segments: list[dict]) -> str | None:
    """Самая частая метка говорящего среди сегментов (по суммарной длительности)."""
    durations: dict[str, float] = {}
    for seg in segments:
        spk = seg.get("speaker")
        if spk is None:
            continue
        start, end = seg.get("start"), seg.get("end")
        durations[spk] = durations.get(spk, 0.0) + ((end - start) if (start is not None and end is not None) else 1.0)
    return max(durations, key=durations.get) if durations else None


class STTClient:
    def __init__(self, base_url: str | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url or URL, timeout=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthy(self) -> bool:
        try:
            r = await self._client.get("/health", timeout=3.0)
            return r.status_code < 500
        except Exception:
            return False

    async def transcribe(self, pcm: bytes, language: str = LANGUAGE, diarize: bool = True,
                         sample_rate: int = SAMPLE_RATE) -> Transcript:
        response = await self._client.post(
            "/v1/audio/transcriptions",
            files={"file": ("audio.wav", pcm_to_wav(pcm, sample_rate), "audio/wav")},
            data={"language": language, "diarize": str(diarize).lower()},
        )
        response.raise_for_status()
        data = response.json()
        speaker = dominant_speaker(data.get("segments", []))
        emb = (data.get("speaker_embeddings") or {}).get(speaker) if speaker is not None else None
        return Transcript(text=data["text"].strip(), speaker=speaker, speaker_embedding=emb)
