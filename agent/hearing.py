"""Слух ГЛаДОС: системный звук (то, что играет в колонках) → текст. Всё ЛОКАЛЬНО.

Схема: запись звука колонок кусками по CHUNK_SECS (WASAPI loopback) → если есть
речь, распознавание локальной моделью Whisper (faster-whisper, на процессоре) →
текст копится ТОЛЬКО В ПАМЯТИ за последние минуты и подмешивается в контекст
разговора («как тебе серия?»). Раз в несколько минут Gemma сводит услышанное в
одну фразу-вывод, которая попадает в память занятий и протокол событий.

Защита приватности (по умолчанию слух ВЫКЛЮЧЕН, включается в трее/чате/дашборде):
  - сырой текст не пишется на диск и не уходит в облако (в контекст Claude не попадает);
  - звонки и мессенджеры (Zoom, Teams, Discord, Telegram…) не слушаем вообще;
  - чувствительные окна (пароли, банк, приватный режим) — как у зрения;
  - не слушаем собственные реплики (на время озвучки запись отбрасывается).
"""
import asyncio
import collections
import logging
import os
import re
import time

import numpy as np

import events_log
import fast_brain
import perception
import settings
from user_memory import UserMemory

logger = logging.getLogger("hearing")

SR = 16000
CHUNK_SECS = 20
MIN_RMS = 0.004                       # тише — считаем тишиной, не гоняем распознавание
KEEP_SECS = 10 * 60                   # сколько минут текста держим в памяти
GIST_EVERY_SECS = 180
MODEL_NAME = os.getenv("GLADOS_STT_MODEL", "small")

_BLOCK_APPS = ("zoom", "teams", "discord", "skype", "telegram", "slack", "webex", "whatsapp",
               "viber", "meet")
_extra = os.getenv("GLADOS_HEARING_BLOCK_APPS", "")
BLOCK_APPS = _BLOCK_APPS + tuple(a.strip().lower() for a in _extra.split(",") if a.strip())

# Типичные «галлюцинации» Whisper на тишине и музыке
_JUNK = re.compile(r"субтитры|субтитрами|dimatorzok|продолжение следует|спасибо за просмотр|"
                   r"подписывайтесь|редактор субтитров", re.I)


class Hearing:
    def __init__(self, memory: UserMemory):
        self._mem = memory
        self._model = None
        self._buf: collections.deque = collections.deque()   # (ts, text)
        self._windows: list[tuple[float, float]] = []        # когда звучала она сама
        self._last_gist = 0.0
        self._gist_from = 0.0

    # ── подавление собственного голоса ─────────────────────────────────────────

    def note_own_speech(self, text: str):
        """Компаньон сообщает, что сейчас озвучит реплику: этот промежуток не слушаем."""
        if settings.load()["voice_muted"]:
            return
        now = time.time()
        self._windows.append((now - 1, now + 3 + len(text) * 0.085))
        self._windows = [w for w in self._windows if w[1] > now - 120]

    def _overlaps_own_speech(self, start: float, end: float) -> bool:
        return any(a < end and b > start for a, b in self._windows)

    # ── доступ к услышанному ──────────────────────────────────────────────────

    def recent_text(self, secs: int = 180, max_chars: int = 900) -> str:
        cutoff = time.time() - secs
        text = " ".join(t for ts, t in self._buf if ts >= cutoff)
        return text[-max_chars:]

    # ── запись и распознавание ──────────────────────────────────────────────────

    def _record(self) -> np.ndarray | None:
        import soundcard as sc
        speaker = sc.default_speaker()
        mic = sc.get_microphone(speaker.name, include_loopback=True)
        with mic.recorder(samplerate=SR, channels=1) as rec:
            data = rec.record(numframes=SR * CHUNK_SECS)
        return data[:, 0].astype(np.float32)

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            logger.info(f"Загружаю модель распознавания речи: {MODEL_NAME}")
            self._model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        return self._model

    def _transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._load_model().transcribe(
            audio, vad_filter=True, beam_size=1, condition_on_previous_text=False)
        out = []
        for s in segments:
            if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0:
                continue
            t = s.text.strip()
            if t and not _JUNK.search(t):
                out.append(t)
        return " ".join(out)

    # ── главный цикл ───────────────────────────────────────────────────────────

    async def run(self):
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Сбой слуха: {e}")
                events_log.log("error", f"слух: {e}")
                await asyncio.sleep(30)

    async def _allowed_now(self) -> bool:
        win = await asyncio.to_thread(perception.foreground)
        if not win:
            return True
        low = (win["title"] + " " + win["app"]).lower()
        if perception.is_sensitive(win) or any(a in low for a in BLOCK_APPS):
            return False
        return True

    async def _step(self):
        if not settings.load()["hearing"]:
            await asyncio.sleep(5)
            return
        if not await self._allowed_now():
            await asyncio.sleep(5)    # звонок или чувствительное окно: не слушаем
            return
        start = time.time()
        audio = await asyncio.to_thread(self._record)
        end = time.time()
        if not settings.load()["hearing"] or not await self._allowed_now():
            return                    # за время записи включилось запретное окно или слух выключили
        if self._overlaps_own_speech(start, end):
            return
        if float(np.sqrt((audio ** 2).mean())) < MIN_RMS:
            return
        text = await asyncio.to_thread(self._transcribe, audio)
        if text:
            self._buf.append((end, text))
        while self._buf and self._buf[0][0] < time.time() - KEEP_SECS:
            self._buf.popleft()
        await self._maybe_gist()

    async def _maybe_gist(self):
        now = time.time()
        if now - self._last_gist < GIST_EVERY_SECS:
            return
        fresh = " ".join(t for ts, t in self._buf if ts >= self._gist_from)
        if len(fresh) < 80:
            return
        self._last_gist, self._gist_from = now, now
        system = ("Ты слух ассистента. По автоматической расшифровке звука с компьютера определи, "
                  "что это: о чём говорят или что играет. Ответь ТОЛЬКО JSON: "
                  '{"summary": "<одна фраза по-русски до 20 слов>"}. Не называй имена людей и не '
                  "цитируй дословно.")
        try:
            data = fast_brain._json_of(await fast_brain._ask(system, fresh[-1500:], timeout=60, background=True,
                                                             max_tokens=100, temperature=0.2))
        except Exception as e:
            logger.warning(f"Не удалось свести услышанное: {e}")
            return
        if data and data.get("summary"):
            summary = str(data["summary"])[:200]
            self._mem.add_activity("звук", "audio", summary)
            events_log.log("hearing", summary)
            logger.info(f"[слух] {summary}")
