"""Тонкая FastAPI-обёртка над WhisperX: транскрипция + диаризация спикеров.

WhisperX не поставляется как HTTP-сервер — оборачиваем его сами, отдавая
OpenAI-совместимый /v1/audio/transcriptions плюс сегменты с метками говорящих.

Диаризация требует gated-моделей pyannote (speaker-diarization-3.1,
segmentation-3.0) — если HF-токен не имеет принятых условий доступа,
пайплайн диаризации не загрузится, но обычная транскрипция продолжит
работать (fail-soft, не блокирует запуск сервиса).
"""

import math
import os
import tempfile

import torch
import whisperx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from whisperx.diarize import DiarizationPipeline

MODEL_NAME = os.environ.get("WHISPERX_MODEL", "large-v3")
LANGUAGE = os.environ.get("WHISPERX_LANGUAGE", "ru")
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
COMPUTE_TYPE = os.environ.get("WHISPERX_COMPUTE_TYPE", "float16" if DEVICE == "cuda" else "int8")
BATCH_SIZE = int(os.environ.get("WHISPERX_BATCH_SIZE", "16"))
HF_TOKEN = os.environ.get("HF_TOKEN")

app = FastAPI(title="WhisperX server")

_asr_model = None
_align_model = None
_align_metadata = None
_diarize_pipeline = None
_diarize_error: str | None = None


@app.on_event("startup")
async def load_models() -> None:
    """Загрузить ASR-модель сразу; alignment — лениво по языку; диаризацию — best-effort."""
    global _asr_model, _diarize_pipeline, _diarize_error

    _asr_model = whisperx.load_model(MODEL_NAME, DEVICE, compute_type=COMPUTE_TYPE)

    try:
        # Явно пин версию 3.1 — новее умолчание whisperx (speaker-diarization-community-1)
        # требует отдельного gated-доступа, которого у нас нет (проверяли только 3.1 + segmentation-3.0).
        _diarize_pipeline = DiarizationPipeline(
            model_name="pyannote/speaker-diarization-3.1", token=HF_TOKEN, device=DEVICE
        )
    except Exception as exc:  # noqa: BLE001 — деградация без диаризации не должна валить сервис
        _diarize_pipeline = None
        _diarize_error = str(exc)
        print(f"[whisperx] diarization pipeline unavailable: {exc}")


def _sanitize_embeddings(embeddings: dict[str, list[float]]) -> dict[str, list[float]]:
    """Отбросить эмбеддинги с NaN/inf — JSON их не поддерживает, а pyannote
    отдаёт NaN для спикеров со слишком коротким для эмбеддинга участком речи
    (мало кадров на модель → np.mean по пустому срезу)."""
    return {
        speaker: vec
        for speaker, vec in embeddings.items()
        if vec and all(math.isfinite(x) for x in vec)
    }


def _get_align_model(language: str):
    """Загрузить (и закэшировать) alignment-модель для конкретного языка."""
    global _align_model, _align_metadata
    if _align_model is None:
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language, device=DEVICE
        )
    return _align_model, _align_metadata


@app.get("/health")
async def health() -> dict:
    """Статус сервиса: загружена ли ASR-модель и доступна ли диаризация."""
    return {
        "status": "ok" if _asr_model is not None else "loading",
        "model": MODEL_NAME,
        "device": DEVICE,
        "diarization_available": _diarize_pipeline is not None,
        "diarization_error": _diarize_error,
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form(LANGUAGE),
    diarize: bool = Form(True),
    min_speakers: int | None = Form(None),
    max_speakers: int | None = Form(None),
) -> JSONResponse:
    """Транскрибировать аудио; при diarize=True и доступном пайплайне — добавить спикеров."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        audio = whisperx.load_audio(tmp_path)
        result = _asr_model.transcribe(audio, batch_size=BATCH_SIZE, language=language)

        align_model, align_metadata = _get_align_model(result["language"])
        result = whisperx.align(
            result["segments"], align_model, align_metadata, audio, DEVICE,
            return_char_alignments=False,
        )

        speaker_embeddings: dict[str, list[float]] = {}
        if diarize and _diarize_pipeline is not None:
            diarize_segments, speaker_embeddings = _diarize_pipeline(
                audio, min_speakers=min_speakers, max_speakers=max_speakers,
                return_embeddings=True,
            )
            speaker_embeddings = _sanitize_embeddings(speaker_embeddings or {})
            result = whisperx.assign_word_speakers(diarize_segments, result)

        segments = [
            {
                "start": seg.get("start"),
                "end": seg.get("end"),
                "text": seg.get("text", "").strip(),
                "speaker": seg.get("speaker"),
            }
            for seg in result["segments"]
        ]
        full_text = " ".join(seg["text"] for seg in segments).strip()

        # Один эмбеддинг на говорящего на весь клип (не по сегментам) — модель
        # pyannote/wespeaker-voxceleb-resnet34-LM, используемая диаризацией
        # внутри пайплайна; см. src/memory/voice_id.py про привязку к людям.
        return JSONResponse({
            "text": full_text,
            "segments": segments,
            "speaker_embeddings": speaker_embeddings,
        })
    finally:
        os.unlink(tmp_path)
