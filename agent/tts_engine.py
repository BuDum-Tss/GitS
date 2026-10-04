"""
TTS-движок для GLaDOS.

Приоритет:
  1. Fish Speech S2-Pro (http://127.0.0.1:8080) — клонирование голоса по GlaDos.wav
  2. edge-tts fallback (ru-RU-SvetlanaNeural, pitch=-25Hz) — если Fish Speech недоступен

Оба пути записывают WAV/MP3 во временный файл и воспроизводят через PowerShell PresentationCore.
"""
import asyncio
import os
import queue
import re
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx

# ── Fish Speech ────────────────────────────────────────────────────────────────

FISH_URL       = os.getenv("FISH_URL", "http://127.0.0.1:8080")
# Референс голоса лежит в проекте; запасной путь к старому месту (GitS) нужен, пока тот проект не удалён
FISH_REFERENCE = Path(os.getenv("FISH_REFERENCE_AUDIO") or (Path(__file__).parent / "resources" / "tts" / "references" / "GlaDos.wav"))
if not FISH_REFERENCE.exists():
    _old = Path(__file__).parent.parent.parent / "GitS" / "resources" / "tts" / "references" / "GlaDos.wav"
    FISH_REFERENCE = _old if _old.exists() else FISH_REFERENCE
FISH_TIMEOUT   = 30.0
FISH_SR        = 44100   # Fish Speech S2-Pro default

# ── edge-tts fallback ─────────────────────────────────────────────────────────

VOICE    = "ru-RU-SvetlanaNeural"
PITCH    = "-25Hz"
RATE     = "-12%"

MAX_CHARS = 600

_q: "queue.Queue[tuple[int, list[str]] | None]" = queue.Queue(maxsize=1)
_current_proc: list[subprocess.Popen] = []
_muted = False
_volume = 1.0                # 0.0-1.0, задаётся из трея/настроек
_gen = [0]                    # поколение реплики — для прерывания устаревшей последовательности
_current_segment_text = [""]  # сегмент, который звучит прямо сейчас (для облачка)


def set_volume(percent: int):
    """Громкость озвучки 0-100. Действует со следующего сегмента."""
    global _volume
    _volume = max(0, min(100, int(percent))) / 100.0


def set_muted(val: bool):
    global _muted
    _muted = val
    if val:
        for p in _current_proc:
            try: p.terminate()
            except Exception: pass

# Кешируем референс при первом использовании
_reference_bytes: bytes | None = None
_fish_available: bool | None   = None   # None = ещё не проверяли


def _get_reference() -> bytes | None:
    global _reference_bytes
    if _reference_bytes is None and FISH_REFERENCE.exists():
        _reference_bytes = FISH_REFERENCE.read_bytes()
        print(f"[tts] Референс GLaDOS: {FISH_REFERENCE} ({len(_reference_bytes):,} б)")
    return _reference_bytes


def _clean(text: str) -> str:
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'#+\s*', '', text)
    text = re.sub(r'`[^`]*`', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    if len(text) <= MAX_CHARS:
        return text
    # Обрезаем по границе предложения, а не посреди слова
    cut = text[:MAX_CHARS]
    last_stop = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
    return cut[:last_stop + 1] if last_stop > MAX_CHARS // 2 else cut


def _pcm_to_wav(pcm: bytes, sample_rate: int = FISH_SR,
                channels: int = 1, sampwidth: int = 2) -> bytes:
    """Обернуть raw PCM int16 в WAV-файл."""
    data_size   = len(pcm)
    byte_rate   = sample_rate * channels * sampwidth
    block_align = channels * sampwidth
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", data_size + 36, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate,
        byte_rate, block_align, sampwidth * 8,
        b"data", data_size,
    )
    return header + pcm


def _play_file(path: str):
    """Воспроизвести аудиофайл (WAV или MP3) через PowerShell PresentationCore
    и ДОЖДАТЬСЯ окончания — нужно, чтобы сегменты озвучивались по очереди,
    а не перекрывали друг друга. Ждёт реальной длительности трека."""
    uri = "file:///" + path.replace("\\", "/")
    ps = (
        "Add-Type -AssemblyName PresentationCore;"
        f"$mp=New-Object system.windows.media.mediaplayer;"
        f"$mp.Open([System.Uri]::new('{uri}'));"
        f"$mp.Volume={_volume:.2f};"
        "$mp.Play();"
        "for ($i=0; $i -lt 100 -and -not $mp.NaturalDuration.HasTimeSpan; $i++) { Start-Sleep -Milliseconds 100 };"
        "if ($mp.NaturalDuration.HasTimeSpan) {"
        "  Start-Sleep -Milliseconds ([int]$mp.NaturalDuration.TimeSpan.TotalMilliseconds + 400)"
        "} else {"
        "  Start-Sleep -Milliseconds 15000"
        "};"
        "$mp.Close()"
    )
    proc = subprocess.Popen(
        ["powershell", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _current_proc.clear()
    _current_proc.append(proc)
    proc.wait()  # блокируем воркер до конца воспроизведения — сегменты идут по очереди


# ── Fish Speech синтез ─────────────────────────────────────────────────────────

async def _fish_ping() -> bool:
    """Проверить доступность Fish Speech."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{FISH_URL}/v1/health")
            return r.status_code < 500
    except Exception:
        return False


async def _fish_synth(text: str) -> str | None:
    """Синтезировать текст через Fish Speech → временный WAV. Вернуть путь или None."""
    import ormsgpack  # lazy import

    ref = _get_reference()
    payload: dict = {
        "text":      text,
        "streaming": True,
        "format":    "wav",
    }
    if ref:
        payload["references"]      = [{"audio": ref, "text": ""}]
        payload["use_memory_cache"] = "on"

    chunks: list[bytes] = []
    try:
        async with httpx.AsyncClient(timeout=FISH_TIMEOUT) as client:
            async with client.stream(
                "POST",
                f"{FISH_URL}/v1/tts",
                content=ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_NUMPY),
                headers={"Content-Type": "application/msgpack"},
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(4096):
                    if chunk:
                        chunks.append(chunk)
    except Exception as e:
        print(f"[tts] Fish Speech ошибка: {e}")
        return None

    raw_pcm = b"".join(chunks)
    if not raw_pcm:
        return None

    # Fish Speech при streaming=True отдаёт raw PCM без заголовка
    wav = _pcm_to_wav(raw_pcm)
    tmp = tempfile.mktemp(suffix=".wav", prefix="glados_fish_")
    with open(tmp, "wb") as f:
        f.write(wav)
    return tmp


# ── edge-tts fallback ─────────────────────────────────────────────────────────

async def _edge_synth(text: str) -> str:
    """Синтезировать текст через edge-tts → MP3."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=VOICE, pitch=PITCH, rate=RATE)
    tmp = tempfile.mktemp(suffix=".mp3", prefix="glados_edge_")
    await communicate.save(tmp)
    return tmp


def _synthesize(clean: str) -> str | None:
    """Синтезировать один сегмент текста в аудиофайл (Fish Speech → fallback edge-tts)."""
    global _fish_available

    if _fish_available is None:
        _fish_available = asyncio.run(_fish_ping())
        if _fish_available:
            print("[tts] Fish Speech доступен — использую голос GLaDOS")
        else:
            print("[tts] Fish Speech недоступен — fallback на edge-tts")

    if _fish_available:
        path = asyncio.run(_fish_synth(clean))
        if path is None:
            path = asyncio.run(_edge_synth(clean))  # разовая ошибка — пробуем edge-tts
        return path
    return asyncio.run(_edge_synth(clean))


# ── фоновый воркер ─────────────────────────────────────────────────────────────

def _worker():
    while True:
        item = _q.get()
        if item is None:
            return
        my_gen, segments = item
        try:
            if _muted:
                continue
            for seg in segments:
                if my_gen != _gen[0] or _muted:
                    break  # нас перебила новая реплика или включили mute

                clean = _clean(seg)
                if not clean:
                    continue

                _current_segment_text[0] = seg
                path = _synthesize(clean)

                if my_gen != _gen[0]:
                    break  # перебили пока синтезировали

                if path and os.path.exists(path):
                    _play_file(path)  # блокирует до конца сегмента
        except Exception as e:
            print(f"[tts] Ошибка: {e}")
        finally:
            _current_segment_text[0] = ""
            _q.task_done()


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_worker, daemon=True, name="tts-worker").start()
    print("[tts] Движок запущен.")


def get_current_segment() -> str:
    """Текст сегмента, который звучит прямо сейчас (для отображения в облачке)."""
    return _current_segment_text[0]


def speak_sequence(segments: list[str]):
    """Поставить несколько сегментов в очередь — озвучиваются по очереди,
    прерывая всё, что говорилось раньше (включая уже начатый сегмент)."""
    segments = [s for s in segments if s and s.strip()]
    if not segments:
        return

    _gen[0] += 1
    my_gen = _gen[0]

    for p in _current_proc:
        try: p.terminate()
        except Exception: pass

    try:
        _q.get_nowait()
        _q.task_done()
    except queue.Empty:
        pass
    try:
        _q.put_nowait((my_gen, segments))
    except queue.Full:
        pass


def speak(text: str):
    """Озвучить один текст целиком (обратная совместимость)."""
    speak_sequence([text])


def synth_file(text: str) -> str | None:
    """Синтезировать текст в аудиофайл (Fish Speech, запасной edge-tts) БЕЗ воспроизведения: для голосовых
    сообщений в Telegram. Блокирующий вызов (гонять через asyncio.to_thread). Файл потом удалить самому."""
    clean = _clean(text)
    return _synthesize(clean) if clean else None
