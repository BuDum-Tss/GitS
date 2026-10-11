"""Вывод речи (ТЗ 02, раздел 9.3; ТЗ 12, VOICE-15): единый интерфейс, через который компаньон говорит, и присутствие.

`SpeechOutput` - то, что умеет любой вывод: сказать, остановить, говорит ли она и какой сегмент звучит. Реализации:
  VoiceModuleSpeech  реплика уходит модулю `voice` (`speak` / `speak.stop`), а «говорит ли она» и текущий сегмент берутся
                     из его событий `voice.speech` (точно, без оценки по длине текста);
  TextOnlySpeech     голоса нет: реплика только в чат дашборда и в `presence` (питомец показывает облачко), `is_speaking()`
                     всегда ложно;
  SpeechSelector     выбирает между ними на каждом вызове: есть подключённый включённый модуль с `voice.output` - голос,
                     нет - текст (выключили модуль голоса: она отвечает текстом, включили: голос вернулся).
Прежний путь через питомца (`LegacyPetSpeech`, файл say.json) удалён вместе с переносом питомца в модуль (ТЗ 13, PET-10).

`PresenceTracker` держит состояние для темы `presence` (кто говорит, эмоция и её время, занята ли, фокус) и сообщает об
изменениях слушателю (шина модулей рассылает тему не чаще 10 раз в секунду, CORE-5).
"""
import asyncio
import logging
import secrets
import time
from typing import Callable, Protocol

logger = logging.getLogger("speech")

SECONDS_PER_CHAR = 0.075      # оценка длительности речи по длине текста, когда голоса нет (облачко питомца без модуля voice)
ESTIMATE_PAD_S = 1.5


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

    DEFAULTS = {"speaking": False, "utterance_id": None, "segment": "", "emotion": "idle", "emotion_ts": 0.0, "busy": False,
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


class TextOnlySpeech:
    """Голоса нет (VOICE-15): реплика только в чат дашборда (его ведёт компаньон) и в `presence`.

    Питомец показывает `presence.segment` как есть: сюда кладётся весь текст реплики, а `speaking` держится оценочное
    время по длине текста (PET-4). `is_speaking()` всегда ложно: очереди речи ждать нечего."""

    def __init__(self, presence: PresenceTracker | None = None):
        self.presence = presence or PresenceTracker()
        self._utterance: str | None = None
        self._timer: asyncio.TimerHandle | None = None

    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._utterance = utterance_id
        self.presence.update(speaking=True, utterance_id=utterance_id, segment=text,
                             **({"emotion": emotion} if emotion else {}))
        self._timer = asyncio.get_running_loop().call_later(
            len(text) * SECONDS_PER_CHAR + ESTIMATE_PAD_S, self._finished, utterance_id)

    def _finished(self, utterance_id: str) -> None:
        if self._utterance == utterance_id:
            self._utterance, self._timer = None, None
            self.presence.update(speaking=False, utterance_id=None, segment="")

    async def stop(self, utterance_id: str | None = None) -> None:
        if utterance_id is not None and utterance_id != self._utterance:
            return
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._utterance is not None:
            self._utterance = None
            self.presence.update(speaking=False, utterance_id=None, segment="")

    def is_speaking(self) -> bool:
        return False

    def current_segment(self) -> str:
        return ""


class VoiceModuleSpeech:
    """Речь через модуль `voice` (VOICE-15): `speak` / `speak.stop` модулю, состояние по его событиям `voice.speech`.

    Реплика считается звучащей с момента отправки `speak` (иначе планировщик очереди речи успел бы запустить следующую, пока
    модуль готовит звук) и до `ended`; если `started` не пришёл за START_TIMEOUT_S, реплика снимается. Пока модуль не
    подключён или выключен, ничего не звучит и состояние сбрасывается."""

    START_TIMEOUT_S = 20.0

    def __init__(self, presence: PresenceTracker | None = None, clock: Callable[[], float] = time.monotonic):
        self.presence = presence or PresenceTracker()
        self._clock = clock
        self.hub = None                              # ModuleHub и ModuleRegistry: ставит ядро (attach)
        self.modules = None
        self._live: dict[str, dict] = {}             # utterance_id -> {sent, started, segment, stopping}

    def attach(self, hub, modules) -> None:
        self.hub, self.modules = hub, modules

    def connection(self):
        """Подключённый включённый модуль с voice.output или None."""
        if self.hub is None:
            return None
        for conn in self.hub.connections():
            if "voice.output" in (conn.manifest or {}).get("capabilities", []) and (
                    self.modules is None or self.modules.is_enabled(conn.module_id)):
                return conn
        return None

    def available(self) -> bool:
        return self.connection() is not None

    # ── SpeechOutput ─────────────────────────────────────────────────────────

    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None:
        conn = self.connection()
        if conn is None:
            raise RuntimeError("модуль голоса не подключён")
        # replace: звучит (или готовится) реплика, которую внимание не просило остановить, - прежнее поведение speak_sequence;
        # после stop() (прерывание вниманием) реплика встаёт в очередь модуля и звучит после дослушанного предложения
        replace = any(not e["stopping"] for e in self._live.values())
        self._live[utterance_id] = {"sent": self._clock(), "started": False, "segment": "", "stopping": False}
        try:
            await conn.send("speak", {"utterance_id": utterance_id, "text": text, "emotion": emotion, "replace": replace})
        except Exception:
            self._live.pop(utterance_id, None)
            raise
        if emotion:
            self.presence.update(emotion=emotion)

    async def stop(self, utterance_id: str | None = None) -> None:
        if utterance_id is not None and utterance_id not in self._live:
            return                                    # такая реплика уже закончилась
        conn = self.connection()
        if conn is None:
            self._reset()
            return
        for uid, entry in self._live.items():
            if utterance_id is None or uid == utterance_id:
                entry["stopping"] = True
        await conn.send("speak.stop", {"utterance_id": utterance_id, "at": "segment_end"})

    def is_speaking(self) -> bool:
        if not self._live:
            return False
        if not self.available():
            self._reset()                             # модуль пропал или выключен: речь оборвалась вместе с ним
            return False
        now = self._clock()
        stale = [uid for uid, e in self._live.items() if not e["started"] and now - e["sent"] > self.START_TIMEOUT_S]
        for uid in stale:
            logger.warning(f"Модуль голоса не начал реплику {uid} за {self.START_TIMEOUT_S:.0f} с: снимаю")
            del self._live[uid]
        return bool(self._live)

    def current_segment(self) -> str:
        started = [e for e in self._live.values() if e["started"]]
        return started[-1]["segment"] if started else ""

    # ── события voice.speech (VOICE-17) ──────────────────────────────────────

    def on_event(self, data: dict) -> None:
        """Событие `voice.speech`: started / segment / ended. Обновляет присутствие (speaking, utterance_id, segment)."""
        uid, phase = data.get("utterance_id"), data.get("phase")
        if not isinstance(uid, str) or not uid:
            return
        if phase in ("started", "segment"):
            entry = self._live.setdefault(uid, {"sent": self._clock(), "started": False, "segment": "", "stopping": False})
            entry["started"], entry["segment"] = True, str(data.get("segment") or "")
        elif phase == "ended":
            self._live.pop(uid, None)
        else:
            return
        self._publish()

    def _publish(self) -> None:
        started = [(uid, e) for uid, e in self._live.items() if e["started"]]
        if started:
            uid, entry = started[-1]
            self.presence.update(speaking=True, utterance_id=uid, segment=entry["segment"])
        else:
            self.presence.update(speaking=False, utterance_id=None, segment="")

    def _reset(self) -> None:
        self._live.clear()
        self._publish()


class SpeechSelector:
    """Выбор вывода речи на каждом вызове (VOICE-15): модуль голоса, если он есть, иначе только текст."""

    def __init__(self, voice: VoiceModuleSpeech, text: TextOnlySpeech):
        self.voice, self.text = voice, text
        self.presence = voice.presence

    def attach(self, hub, modules) -> None:
        self.voice.attach(hub, modules)

    def on_event(self, data: dict) -> None:
        self.voice.on_event(data)

    def _active(self) -> SpeechOutput:
        return self.voice if self.voice.available() else self.text

    async def speak(self, utterance_id: str, text: str, emotion: str | None, priority: int) -> None:
        if self.voice.available():
            try:
                await self.voice.speak(utterance_id, text, emotion, priority)
                return
            except Exception as e:
                logger.warning(f"Модуль голоса не принял реплику, остаётся текст: {e}")
        await self.text.speak(utterance_id, text, emotion, priority)

    async def stop(self, utterance_id: str | None = None) -> None:
        await self.voice.stop(utterance_id)
        await self.text.stop(utterance_id)

    def is_speaking(self) -> bool:
        return self.voice.is_speaking()

    def current_segment(self) -> str:
        return self.voice.current_segment()
