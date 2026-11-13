"""Сервис синтеза речи ESpeech-TTS-1 (F5-TTS, русский) для модуля голоса.

Отдельный процесс в своём окружении (Python 3.12 + CUDA torch, см. README.md). Контракт:
  GET  /health  -> 200 {"ok": true}, когда модель загружена и прогрета; до этого 503
  POST /tts     -> JSON {text, ref_audio | ref_audio_b64, ref_text} -> audio/wav (mono, 24 кГц)
`ref_audio_b64` это WAV референса в base64 (годится и для контейнера: пути клиента сервер не видит); сервер кладёт его в кэш
по хэшу содержимого. `ref_audio` это путь к референсу на этой же машине (прежний способ), `ref_text` его дословный текст.
Настройки окружения: F5_HOST (по умолчанию 127.0.0.1, в контейнере 0.0.0.0), F5_PORT, F5_CKPT, F5_VOCAB, F5_WARMUP_DIR,
F5_REF_CACHE (папка кэша референсов), F5_ACCENT_DIR (модели RUAccent), F5_NFE_STEP, F5_DEVICE.
Ударения расставляет RUAccent; слова с «+» перед ударной гласной остаются как есть.
"""
import asyncio
import base64
import binascii
import hashlib
import io
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import soundfile as sf
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
HOST = os.getenv("F5_HOST") or "127.0.0.1"
PORT = int(os.getenv("F5_PORT", "8771"))
CKPT = Path(os.getenv("F5_CKPT") or HERE / "weights" / "espeech" / "espeech_tts_256k.pt")
VOCAB = Path(os.getenv("F5_VOCAB") or HERE / "weights" / "espeech" / "vocab.txt")
NFE_STEP = int(os.getenv("F5_NFE_STEP", "16"))       # меньше шагов диффузии: синтез быстрее почти вдвое, качество чуть ниже (32 было прежним)
WARMUP_DIR = Path(os.getenv("F5_WARMUP_DIR") or HERE.parent.parent / "resources" / "GlaDOS" / "voice")
REF_CACHE = Path(os.getenv("F5_REF_CACHE") or Path(tempfile.gettempdir()) / "f5_refs")   # референсы из ref_audio_b64
DEVICE = os.getenv("F5_DEVICE", "cuda")

logger = logging.getLogger("f5")
app = FastAPI()
_state: dict = {"tts": None, "accent": None}
_lock = threading.Lock()                       # модель одна, запросы идут по очереди


class TtsRequest(BaseModel):
    text: str
    ref_audio: str = ""                  # путь к файлу на машине сервера (прежний способ)
    ref_audio_b64: str = ""              # WAV в base64: приоритетнее пути
    ref_text: str
    speed: float = 1.0


def resolve_ref(ref_audio: str, ref_audio_b64: str, cache_dir: Path | None = None) -> str | None:
    """Путь к файлу референса для синтеза или None, если референса нет. `ref_audio_b64` (WAV в base64) пишется в кэш под именем
    по хэшу содержимого и не пересоздаётся, пока файл есть; иначе берётся путь `ref_audio`, если такой файл существует."""
    if ref_audio_b64:
        try:
            data = base64.b64decode(ref_audio_b64, validate=True)
        except (binascii.Error, ValueError):
            data = b""
        if data:
            folder = cache_dir or REF_CACHE
            target = folder / f"ref_{hashlib.sha256(data).hexdigest()[:24]}.wav"
            if not target.is_file():
                folder.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)                                   # запись целиком или никак: читатель не увидит обрубок
            return str(target)
    if ref_audio and Path(ref_audio).is_file():
        return ref_audio
    return None


_accent_cache: dict[str, str] = {}


def _accent_safe(text: str) -> str:
    """RUAccent иногда падает на отдельных словах (ValueError: нет token_type_ids), и тогда весь запрос давал 500, а голос
    падал на запасной движок. Ошибка на предложении оставляет это предложение без ударений, остальные размечаются."""
    try:
        return _state["accent"].process_all(text)
    except Exception as e:
        logger.warning(f"ударения не расставлены целиком ({type(e).__name__}), разбираю по предложениям")
    parts = re.split(r"(?<=[.!?…])\s+", text)
    out = []
    for part in parts:
        try:
            out.append(_state["accent"].process_all(part))
        except Exception:
            out.append(part)
    return " ".join(out)


def _accent(text: str) -> str:
    """Ударения; результат запоминается (референс одинаков в каждом запросе, повторные реплики тоже)."""
    hit = _accent_cache.get(text)
    if hit is None:
        hit = _accent_safe(text)
        if len(_accent_cache) > 512:
            _accent_cache.clear()
        _accent_cache[text] = hit
    return hit


def _warmup(tts) -> None:
    """Один холостой синтез на референсе персонажа: первый вызов компилирует ядра CUDA, пусть это случится до первой реплики."""
    ref, txt = WARMUP_DIR / "espeech_ref.wav", WARMUP_DIR / "espeech_ref.txt"
    if not (ref.is_file() and txt.is_file()):
        return
    try:
        t = time.time()
        ref_text = txt.read_text(encoding="utf-8").strip()
        with _lock:
            tts.infer(ref_file=str(ref), ref_text=_accent(ref_text), gen_text=_accent("Проверка связи."),
                      nfe_step=NFE_STEP, seed=1)
        logger.info("прогрев за %.1f с", time.time() - t)
    except Exception:
        logger.exception("прогрев не удался")


def _load() -> None:
    """Загрузка модели и прогрев (первый вызов компилирует ядра и занимает заметное время)."""
    from f5_tts.api import F5TTS
    from ruaccent import RUAccent

    acc = RUAccent()
    accent_dir = os.getenv("F5_ACCENT_DIR")                  # куда RUAccent кладёт свои модели (по умолчанию в папку пакета; в контейнере пакет не для записи)
    if accent_dir:
        Path(accent_dir).mkdir(parents=True, exist_ok=True)
    acc.load(omograph_model_size="turbo3.1", use_dictionary=True, workdir=accent_dir or None)
    _state["accent"] = acc
    t = time.time()
    tts = F5TTS(model="F5TTS_v1_Base", ckpt_file=str(CKPT), vocab_file=str(VOCAB), device=DEVICE)
    logger.info("модель загружена за %.1f с", time.time() - t)
    _warmup(tts)
    _state["tts"] = tts


def _synth(req: TtsRequest, ref_file: str) -> bytes:
    with _lock:
        wav, sr, _ = _state["tts"].infer(ref_file=ref_file, ref_text=_accent(req.ref_text), gen_text=_accent(req.text),
                                         nfe_step=NFE_STEP, speed=req.speed, seed=1)
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.get("/health")
async def health():
    if _state["tts"] is None:
        return JSONResponse({"ok": False, "loading": True}, status_code=503)
    return {"ok": True}


@app.post("/tts")
async def tts(req: TtsRequest):
    if _state["tts"] is None:
        return JSONResponse({"error": "loading"}, status_code=503)
    try:
        ref_file = resolve_ref(req.ref_audio, req.ref_audio_b64) if req.text.strip() else None
    except OSError as e:
        logger.exception("референс не записан в кэш")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if not ref_file:
        return JSONResponse({"error": "text or ref_audio missing"}, status_code=400)
    try:
        data = await asyncio.to_thread(_synth, req, ref_file)
    except Exception as e:                                               # сбой синтеза не должен ронять сервис
        logger.exception("синтез не удался")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(content=data, media_type="audio/wav")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    threading.Thread(target=_load, daemon=True).start()                  # /health отвечает 503, пока модель грузится
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
