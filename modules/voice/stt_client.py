"""STT-клиент: аудио → текст. Основной путь: WhisperX-сервер (docker/whisperx) с диаризацией и голосовыми эмбеддингами.
Запасной путь (без docker): локальный faster-whisper на процессоре (local_stt.py), без диаризации и без узнавания голосов.

Сервер OpenAI-совместим (/v1/audio/transcriptions) и добавляет диаризацию. Адрес берётся из WHISPERX_URL
(по умолчанию http://127.0.0.1:9000). Режим: при старте и при ошибке соединения проверяется `/health`; пока сервер
недоступен, распознаём локально; проверка повторяется раз в RECHECK_S секунд, и когда сервер появляется, возвращаемся на него.
"""
import asyncio
import io
import logging
import os
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass

import httpx

import local_stt

logger = logging.getLogger("stt")

SAMPLE_RATE = 16_000
URL = os.getenv("WHISPERX_URL", "http://127.0.0.1:9000")
LANGUAGE = os.getenv("WHISPERX_LANGUAGE", "ru")
RECHECK_S = 60.0                  # сервер недоступен: повторная проверка не чаще раза в минуту

MODE_SERVER = "server"
MODE_LOCAL = "local"
MODE_UNKNOWN = "unknown"

LABEL_SERVER = "распознавание: WhisperX (сервер), с узнаванием голосов"
LABEL_LOCAL = "распознавание: локально (CPU), без узнавания голосов"
LABEL_UNKNOWN = "распознавание: ещё не проверено"


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
    def __init__(self, base_url: str | None = None, *, local: local_stt.LocalWhisper | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, on_change: Callable[[dict], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        """local - запасной распознаватель (по умолчанию общий на процесс); on_change(status) - сменился режим или состояние
        модели (вызывается один раз на переход: журнал и состояние модуля); clock - время (тесты подменяют)."""
        self._client = httpx.AsyncClient(base_url=base_url or URL, timeout=60.0, transport=transport)
        self._local = local if local is not None else local_stt.shared()
        self._on_change = on_change
        self._clock = clock
        self._server_ok: bool | None = None          # None: ещё не проверяли
        self._checked_at = float("-inf")
        self._last_key: tuple | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── режим и подписи ──────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        if self._server_ok is None:
            return MODE_UNKNOWN
        return MODE_SERVER if self._server_ok else MODE_LOCAL

    @property
    def diarized(self) -> bool:
        """Есть ли диаризация и голосовые отпечатки (только у сервера WhisperX)."""
        return self.mode == MODE_SERVER

    def label(self) -> str:
        if self.mode == MODE_SERVER:
            return LABEL_SERVER
        if self.mode == MODE_LOCAL:
            if self._local.state == "загружена":
                return LABEL_LOCAL
            return f"{LABEL_LOCAL}; модель {self._local.model_name} не загружена, речь не распознаётся"
        return LABEL_UNKNOWN

    def short(self) -> str:
        if self.mode == MODE_SERVER:
            return "WhisperX"
        if self.mode == MODE_LOCAL:
            return "локально (CPU)" if self._local.state == "загружена" else "локально, модель не загружена"
        return "не проверено"

    def status(self) -> dict:
        return {"mode": self.mode, "label": self.label(), "short": self.short(), "diarized": self.diarized,
                "model": self._local.model_name, "model_state": self._local.state}

    def _notify(self) -> None:
        """Сообщить о переходе (ключ: режим и состояние модели), один раз на переход."""
        key = (self.mode, self._local.state == "не загружена")     # «не проверена» и «загружена» - один переход
        if key == self._last_key:
            return
        self._last_key = key
        if self._on_change is not None:
            try:
                self._on_change(self.status())
            except Exception as e:
                logger.debug(f"Смена режима распознавания: {e}")

    # ── проверка сервера ─────────────────────────────────────────────────────

    async def healthy(self) -> bool:
        try:
            r = await self._client.get("/health", timeout=3.0)
            return r.status_code < 500
        except Exception:
            return False

    async def probe(self) -> str:
        """Проверить сервер сейчас. Если он недоступен, заодно проверяем запасную модель (без скачивания). Вернуть режим."""
        ok = await self.healthy()
        self._server_ok, self._checked_at = ok, self._clock()
        if not ok:
            await asyncio.to_thread(self._local.check)
        self._notify()
        return self.mode

    async def watch(self) -> None:
        """Фоновый цикл: проверка при старте, затем раз в RECHECK_S (возврат на сервер и обнаружение его падения)."""
        while True:
            try:
                await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Проверка WhisperX: {e}")
            await asyncio.sleep(RECHECK_S)

    async def _server_usable(self) -> bool:
        if self._server_ok is None:
            await self.probe()
        elif self._server_ok is False and self._clock() - self._checked_at >= RECHECK_S:
            await self.probe()
        return bool(self._server_ok)

    def _server_down(self, why: str) -> None:
        self._server_ok, self._checked_at = False, self._clock()
        logger.info(f"Сервер WhisperX не отвечает ({why[:80]}): распознаю локально")

    # ── распознавание ────────────────────────────────────────────────────────

    async def transcribe(self, pcm: bytes, language: str = LANGUAGE, diarize: bool = True,
                         sample_rate: int = SAMPLE_RATE) -> Transcript:
        if await self._server_usable():
            try:
                return await self._transcribe_server(pcm, language, diarize, sample_rate)
            except httpx.TransportError as e:
                self._server_down(f"{type(e).__name__}: {e}")
            except httpx.HTTPStatusError as e:
                if e.response.status_code < 500:
                    raise
                self._server_down(f"HTTP {e.response.status_code}")
            self._notify()
        return await self._transcribe_local(pcm, sample_rate)

    async def _transcribe_server(self, pcm: bytes, language: str, diarize: bool, sample_rate: int) -> Transcript:
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

    async def _transcribe_local(self, pcm: bytes, sample_rate: int) -> Transcript:
        """Запасной путь: без диаризации и без голосовых отпечатков (voice_id в этом режиме отключён)."""
        try:
            text = await asyncio.to_thread(self._local.transcribe_pcm, pcm, sample_rate)
        finally:
            self._notify()
        return Transcript(text=text.strip(), speaker=None, speaker_embedding=None)
