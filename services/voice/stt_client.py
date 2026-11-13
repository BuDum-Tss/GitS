"""STT-клиент: аудио → текст, локально, без сети. Основной движок GigaAM, запасной faster-whisper int8 (оба в local_stt.py,
выбор: `GITS_STT_ENGINE`, язык: `GITS_STT_LANGUAGE`). Диаризации и узнавания голосов по отпечатку нет: распознаётся только текст.

Клиент держит состояние модели для дашборда (загружена или нет) и сообщает о его смене один раз на переход (`on_change`).
Проверка модели при старте и раз в RECHECK_S секунд (`watch`): веса могли появиться в кэше, пока модуль работал.
"""
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

import local_stt

logger = logging.getLogger("stt")

SAMPLE_RATE = 16_000
LANGUAGE = local_stt.LANGUAGE
RECHECK_S = 60.0                  # модель не загружена: повторная проверка не чаще раза в минуту

MODE_LOCAL = "local"
MODE_UNKNOWN = "unknown"

LABEL_LOCAL = "распознавание: локально (CPU)"
LABEL_UNKNOWN = "распознавание: ещё не проверено"


@dataclass
class Transcript:
    text: str


class STTClient:
    def __init__(self, *, local: local_stt.LocalWhisper | None = None, on_change: Callable[[dict], None] | None = None):
        """local - распознаватель (по умолчанию общий на процесс: GigaAM, при неудаче faster-whisper); on_change(status) -
        сменилось состояние модели (один вызов на переход: журнал и состояние модуля)."""
        self._local = local if local is not None else local_stt.shared()
        self._on_change = on_change
        self._checked = False
        self._last_key: tuple | None = None

    async def aclose(self) -> None:
        return None

    # ── режим и подписи ──────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return MODE_LOCAL if self._checked or self._local.state != "не проверена" else MODE_UNKNOWN

    def label(self) -> str:
        if self.mode == MODE_UNKNOWN:
            return LABEL_UNKNOWN
        if self._local.state == "загружена":
            return f"{LABEL_LOCAL}, модель {self._model_name()}"
        return f"{LABEL_LOCAL}; модель {self._model_name()} не загружена, речь не распознаётся"

    def short(self) -> str:
        if self.mode == MODE_UNKNOWN:
            return "не проверено"
        return "локально (CPU)" if self._local.state == "загружена" else "локально, модель не загружена"

    def _model_name(self) -> str:
        return getattr(self._local, "active_model", self._local.model_name)     # GigaAM или Whisper, если откатились

    def status(self) -> dict:
        return {"mode": self.mode, "label": self.label(), "short": self.short(),
                "model": self._model_name(), "model_state": self._local.state}

    def _notify(self) -> None:
        """Сообщить о переходе (состояние и имя модели), один раз на переход."""
        key = (self._local.state, self._model_name())
        if key == self._last_key:
            return
        self._last_key = key
        if self._on_change is not None:
            try:
                self._on_change(self.status())
            except Exception as e:
                logger.debug(f"Смена состояния распознавания: {e}")

    # ── проверка модели ──────────────────────────────────────────────────────

    async def probe(self) -> str:
        """Проверить, открывается ли модель (без скачивания). Вернуть режим."""
        await asyncio.to_thread(self._local.check)
        self._checked = True
        self._notify()
        return self.mode

    async def watch(self) -> None:
        """Фоновый цикл: проверка при старте, затем раз в RECHECK_S, пока модель не загружена."""
        while True:
            try:
                if not self._checked or self._local.state != "загружена":
                    await self.probe()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Проверка модели распознавания: {e}")
            await asyncio.sleep(RECHECK_S)

    # ── распознавание ────────────────────────────────────────────────────────

    async def transcribe(self, pcm: bytes, sample_rate: int = SAMPLE_RATE) -> Transcript:
        try:
            text = await asyncio.to_thread(self._local.transcribe_pcm, pcm, sample_rate)
        finally:
            self._checked = True
            self._notify()
        return Transcript(text=text.strip())
