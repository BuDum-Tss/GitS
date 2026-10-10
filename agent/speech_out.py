"""Вывод речи (ТЗ 02, раздел 9.3): единый интерфейс, через который компаньон говорит, и присутствие.

`SpeechOutput` - то, что умеет любой вывод: сказать, остановить, ответить, говорит ли она и какой сегмент звучит.
Сейчас реализация одна, `LegacyPetSpeech` - прежний путь: реплика пишется в `memory/say.json`, дашборд отдаёт её питомцу
(`desktop_pet.py`), а тот озвучивает и показывает облачко. Ядро питомца не слышит, поэтому «говорит ли она» оценивается
по длине текста. После ТЗ 12 озвучку и воспроизведение берёт модуль `voice`, и появится реализация через него; компаньон
при этом не меняется.

`PresenceTracker` держит состояние для темы `presence` (кто говорит, эмоция, занята ли, фокус) и сообщает об изменениях
слушателю (шина модулей рассылает тему не чаще 10 раз в секунду, CORE-5).
"""
import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Callable, Protocol

import settings

logger = logging.getLogger("speech")


def new_utterance_id() -> str:
    """Id реплики: `u-` + 8 hex (CORE-28)."""
    return "u-" + secrets.token_hex(4)


class SpeechOutput(Protocol):
    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None: ...
    async def stop(self, utterance_id: str | None = None) -> None: ...
    def is_speaking(self) -> bool: ...
    def current_segment(self) -> str: ...


class PresenceTracker:
    """Состояние присутствия для модулей (тема `presence`, ТЗ 01 раздел 8).

    update(**поля) меняет только известные поля; если что-то изменилось, вызывает listener(снимок). listener - синхронная
    функция (шина модулей), она сама решает, как рассылать."""

    DEFAULTS = {"speaking": False, "utterance_id": None, "segment": "", "emotion": "idle", "busy": False,
                "focus": "idle", "user_speaking": False, "last_user_ts": 0.0, "last_say": "", "last_say_ts": 0.0}

    def __init__(self, listener: Callable[[dict], None] | None = None):
        self.state = dict(self.DEFAULTS)
        self.listener = listener

    def snapshot(self) -> dict:
        return dict(self.state)

    def update(self, **fields) -> bool:
        changed = False
        for key, value in fields.items():
            if key in self.state and self.state[key] != value:
                self.state[key] = value
                changed = True
        if changed and self.listener is not None:
            try:
                self.listener(self.snapshot())
            except Exception as e:
                logger.warning(f"Рассылка присутствия: {e}")
        return changed


class LegacyPetSpeech:
    """Прежний путь озвучки через питомца (CORE-27): say.json + оценка длительности по длине текста."""

    SECONDS_PER_CHAR = 0.075
    PAD_S = 1.5

    def __init__(self, memory_dir: str | Path, presence: PresenceTracker | None = None,
                 now: Callable[[], float] = time.time):
        self._dir = Path(memory_dir)
        self.presence = presence or PresenceTracker()
        self._now = now
        self._say_id = 0
        self._speak_until = 0.0
        self._utterance: str | None = None
        self._text = ""
        self._payload: dict = {}                    # последняя запись say.json: к ней добавляется метка остановки
        self._timer: asyncio.TimerHandle | None = None

    def _write(self, payload: dict):
        try:
            (self._dir / "say.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            logger.warning(f"say.json: {e}")

    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None:
        self._say_id += 1
        now = self._now()
        muted = bool(settings.load()["voice_muted"])
        if not muted:
            self._speak_until = now + len(text) * self.SECONDS_PER_CHAR + self.PAD_S   # озвучка идёт в процессе питомца
        # текст пишется и при заглушённом голосе: питомец показывает облачко
        self._payload = {"id": self._say_id, "text": text, "ts": now}
        await asyncio.to_thread(self._write, self._payload)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if muted:
            return
        self._utterance, self._text = utterance_id, text
        self.presence.update(speaking=True, utterance_id=utterance_id, segment=text,
                             **({"emotion": emotion} if emotion else {}))
        self._timer = asyncio.get_running_loop().call_later(self._speak_until - now, self._finished, utterance_id)

    def _finished(self, utterance_id: str):
        if self._utterance == utterance_id:
            self._utterance, self._timer = None, None
            self.presence.update(speaking=False, utterance_id=None, segment="")

    async def stop(self, utterance_id: str | None = None) -> None:
        """Прервать речь (ATT-17, ATT-18). Снимается признак «говорит», а в say.json кладётся метка `stop`: питомец, увидев
        новую метку, не озвучивает следующие предложения (идущее договаривает: остановка на границе предложения). Настоящая
        остановка с эхо-защитой появится с модулем voice (ТЗ 12)."""
        if utterance_id is not None and utterance_id != self._utterance:
            return
        if self._payload:
            self._payload = {**self._payload, "stop": self._now()}
            await asyncio.to_thread(self._write, self._payload)
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._speak_until = 0.0
        self._utterance = None
        self.presence.update(speaking=False, utterance_id=None, segment="")

    def is_speaking(self) -> bool:
        return self._now() < self._speak_until

    def current_segment(self) -> str:
        return self._text if self.is_speaking() else ""
