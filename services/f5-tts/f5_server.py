"""Сервис синтеза речи ESpeech-TTS-1 (F5-TTS, русский) для модуля голоса.

Отдельный процесс в своём окружении (Python 3.12 + CUDA torch, см. README.md). Контракт:
  GET  /health  -> 200 {"ok": true}, когда модель загружена и прогрета; до этого 503
  POST /tts     -> JSON {text, ref_audio, ref_text} -> audio/wav (mono, 24 кГц)
`ref_audio` это путь к референсу на этой же машине (сервис слушает только 127.0.0.1), `ref_text` его дословный текст.
Ударения расставляет RUAccent; слова с «+» перед ударной гласной остаются как есть.
"""
import asyncio
import io
import logging
import os
import threading
import time
from pathlib import Path

import soundfile as sf
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.getenv("F5_PORT", "8771"))
CKPT = Path(os.getenv("F5_CKPT") or HERE / "weights" / "espeech" / "espeech_tts_256k.pt")
VOCAB = Path(os.getenv("F5_VOCAB") or HERE / "weights" / "espeech" / "vocab.txt")
NFE_STEP = int(os.getenv("F5_NFE_STEP", "16"))       # меньше шагов диффузии: синтез быстрее почти вдвое, качество чуть ниже (32 было прежним)
WARMUP_DIR = HERE.parent.parent / "resources" / "GlaDOS" / "voice"
DEVICE = os.getenv("F5_DEVICE", "cuda")

logger = logging.getLogger("f5")
app = FastAPI()
_state: dict = {"tts": None, "accent": None}
_lock = threading.Lock()                       # модель одна, запросы идут по очереди


class TtsRequest(BaseModel):
    text: str
    ref_audio: str
    ref_text: str
    speed: float = 1.0


_accent_cache: dict[str, str] = {}


def _accent(text: str) -> str:
    """Ударения; результат запоминается (референс одинаков в каждом запросе, повторные реплики тоже)."""
    hit = _accent_cache.get(text)
    if hit is None:
        hit = _state["accent"].process_all(text)
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
    acc.load(omograph_model_size="turbo3.1", use_dictionary=True)
    _state["accent"] = acc
    t = time.time()
    tts = F5TTS(model="F5TTS_v1_Base", ckpt_file=str(CKPT), vocab_file=str(VOCAB), device=DEVICE)
    logger.info("модель загружена за %.1f с", time.time() - t)
    _warmup(tts)
    _state["tts"] = tts


def _synth(req: TtsRequest) -> bytes:
    with _lock:
        wav, sr, _ = _state["tts"].infer(ref_file=req.ref_audio, ref_text=_accent(req.ref_text), gen_text=_accent(req.text),
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
    if not req.text.strip() or not Path(req.ref_audio).is_file():
        return JSONResponse({"error": "text or ref_audio missing"}, status_code=400)
    try:
        data = await asyncio.to_thread(_synth, req)
    except Exception as e:                                               # сбой синтеза не должен ронять сервис
        logger.exception("синтез не удался")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return Response(content=data, media_type="audio/wav")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    threading.Thread(target=_load, daemon=True).start()                  # /health отвечает 503, пока модель грузится
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
