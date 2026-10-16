"""Слух ГЛаДОС: системный звук (то, что играет в колонках) → текст. Всё ЛОКАЛЬНО.

Перенесено из ядра в модуль голоса (ТЗ 12, VOICE-12): логика распознавания без изменений.

Схема: запись звука колонок кусками по CHUNK_SECS (WASAPI loopback) → если есть
речь, распознавание локальной моделью Whisper (faster-whisper, на процессоре) →
текст копится ТОЛЬКО В ПАМЯТИ за последние минуты (инструмент `recent_audio` отдаёт его
ядру по запросу). Раз в несколько минут Gemma сводит услышанное в одну фразу-вывод:
она уходит в ядро событием `voice.heard` (VOICE-14); ядро само пишет её в память занятий.

Защита приватности (по умолчанию слух ВЫКЛЮЧЕН, включается в трее/чате/дашборде):
  - сырой текст не пишется на диск и не уходит в облако (в контекст Claude не попадает);
  - звонки и мессенджеры (Zoom, Teams, Discord, Telegram…) не слушаем вообще;
  - чувствительные окна (пароли, банк, приватный режим) — как у зрения;
  - не слушаем собственные реплики (на время озвучки запись отбрасывается).
"""
import asyncio
import collections
import logging
import time

import numpy as np

from gits_sdk import host
import local_stt
import window
from local_stt import _JUNK  # noqa: F401  (прежнее место: mic_listener берёт фильтр отсюда)
from envcompat import env

logger = logging.getLogger("hearing")

SR = 16000
CHUNK_SECS = 20
MIN_RMS = 0.004                       # тише — считаем тишиной, не гоняем распознавание
KEEP_SECS = 10 * 60                   # сколько минут текста держим в памяти
GIST_EVERY_SECS = 180

_BLOCK_APPS = ("zoom", "teams", "discord", "skype", "telegram", "slack", "webex", "whatsapp",
               "viber", "meet")
_extra = env("GITS_HEARING_BLOCK_APPS", "")
BLOCK_APPS = _BLOCK_APPS + tuple(a.strip().lower() for a in _extra.split(",") if a.strip())


class Hearing:
    def __init__(self, settings_get, on_gist=None, foreground=window.foreground, is_sensitive=window.is_sensitive,
                 engine=None):
        """settings_get() - общие настройки ядра (`hearing`, `voice_muted`); on_gist(summary) - async, сводка готова
        (событие `voice.heard`); foreground() - активное окно {title, app}; is_sensitive(окно) - личное ли оно;
        engine - распознаватель (по умолчанию общий с микрофоном: модель грузится один раз)."""
        self._settings_get = settings_get
        self._on_gist = on_gist
        self._foreground, self._is_sensitive = foreground, is_sensitive
        self._engine = engine if engine is not None else local_stt.shared()
        self._buf: collections.deque = collections.deque()   # (ts, text)
        self._windows: list[tuple[float, float]] = []        # когда звучала она сама
        self._last_gist = 0.0
        self._gist_from = 0.0

    # ── подавление собственного голоса ─────────────────────────────────────────

    def note_own_speech(self, text: str):
        """Модуль сообщает, что сейчас звучит её предложение: этот промежуток не слушаем."""
        if self._settings_get().get("voice_muted"):
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

    def _transcribe(self, audio: np.ndarray) -> str:
        """Распознавание общей локальной моделью (local_stt): та же модель, что у запасного пути микрофона."""
        return self._engine.transcribe_array(audio)

    # ── главный цикл ───────────────────────────────────────────────────────────

    async def run(self):
        while True:
            try:
                await self._step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Сбой слуха: {e}")
                host.log_nowait("error", f"слух: {e}")
                await asyncio.sleep(30)

    async def _allowed_now(self) -> bool:
        """VOICE-13: слух включён в общих настройках, а активное окно не звонок и не чувствительное."""
        if not self._settings_get().get("hearing"):
            return False
        win = await asyncio.to_thread(self._foreground)
        if not win:
            return True
        low = (win["title"] + " " + win["app"]).lower()
        if self._is_sensitive(win) or any(a in low for a in BLOCK_APPS):
            return False
        return True

    async def _step(self):
        if not self._settings_get().get("hearing"):
            await asyncio.sleep(5)
            return
        if not await self._allowed_now():
            await asyncio.sleep(5)    # звонок или чувствительное окно: не слушаем
            return
        start = time.time()
        audio = await asyncio.to_thread(self._record)
        end = time.time()
        if not await self._allowed_now():
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
            data = await host.llm_json(system, fresh[-1500:], timeout=60, background=True, max_tokens=100, temperature=0.2)
        except Exception as e:
            logger.warning(f"Не удалось свести услышанное: {e}")
            return
        if data and data.get("summary"):
            summary = str(data["summary"])[:200]
            host.log_nowait("hearing", summary)
            logger.info(f"[слух] {summary}")
            if self._on_gist is not None:
                await self._on_gist(summary)
