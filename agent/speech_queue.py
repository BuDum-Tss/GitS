"""Очередь речи (ТЗ 03, раздел 7): единственное место, откуда звучит самостоятельная и ответная речь.

Все пути (ответ разговора, `say` модулей, факты менеджера внимания, проактивные реплики, вопросы) кладут сюда кандидатов
`SpeechCandidate`; планировщик `SpeechQueue.pump()` выбирает лучшего и запускает его, когда выполнены условия:
  1. сейчас она не говорит и не готовит реплику;
  2. пользователь не говорит и закончил говорить не меньше 1,5 с назад (для ответов на его обращение достаточно «не говорит»:
     он ждёт ответа, а не заканчивает фразу);
  3. нет заданного вопроса, на который ждут ответа (кроме кандидатов `conversation` и `question`);
  4. с последней самостоятельной реплики прошло не меньше `min_gap` (тихо 90 с, обычно 30, болтливо 10); не действует для
     `conversation`, `question`, `announce`, кандидатов с приоритетом 85 и выше и готовых реплик модулей (`module:*`,
     verbatim: модуль сам задаёт им темп).
Объявления о запусках (`announce`, правило 14 ТЗ: каждое должно прозвучать): приоритет 75, срок 180 с, пауза между репликами и
повторы недавно сказанного их не задерживают и не снимают, вопрос в ожидании не мешает; текущую реплику они не обрывают
(кроме «болтовни»), а звучат сразу после неё.
Лучший кандидат: больший приоритет, при равенстве старший. Устаревшие (`now > expires`) и повторы недавно сказанного
удаляются с записью в журнал. Приоритет 85 и выше прерывает звучащую речь, если она ниже на запас прерывания и более; готовый
к D3 кандидат (60 и выше) прерывает «болтовню» (звучащее ниже 35). Прерванный кандидат не возвращается.

Класс не знает ни компаньона, ни настроек: всё нужное ему дают функции-источники, поэтому он проверяется с фиктивными
часами и заглушкой вывода речи, без `sleep`.
"""
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

import flow_trace
from typing import Any, Awaitable, Callable

logger = logging.getLogger("speech_queue")

MIN_GAP_S = {"quiet": 90.0, "normal": 30.0, "chatty": 10.0}      # ATT-14, пункт 4
USER_QUIET_S = 1.5                                                # ATT-14, пункт 2
MAX_QUEUE = 50
INTERRUPT_P = 85                  # ATT-17: с такого приоритета речь прерывает звучащую
CHATTER_P = 35                    # «болтовня»: звучащее ниже этого прерывается кандидатом D3 (priority >= 60)
D3_P = 60

SOURCES = ("conversation", "module", "attention", "proactive", "question", "announce")
DEFAULT_TTL_S = {"conversation": 120.0, "verbatim": 20.0, "fact": 600.0, "proactive": 60.0, "question": 120.0,
                 "announce": 180.0}
ANNOUNCE_P = 75                   # AN-1: выше болтовни и D2, ниже прерываний (>= 85)


def new_candidate_id() -> str:
    return "s-" + secrets.token_hex(4)


@dataclass
class SpeechCandidate:
    id: str
    source: str                    # "conversation" | "module:<id>" | "attention:<item id>" | "proactive" | "question" | "announce:<id>"
    mode: str                      # "verbatim" | "fact"
    text: str
    emotion: str | None
    priority: int                  # 0..100
    created: float
    expires: float
    key: str | None = None         # одинаковые key: остаётся новейший
    user_text: str = ""            # fact: что сказал пользователь (для контекста реплики)
    must: bool = False             # fact: промолчать нельзя (итог просьбы)
    question: bool = False         # fact: вопрос к пользователю
    channel: Any = None            # канал обращения (conversation_io.Channel), ставится на время реплики
    item_id: str | None = None     # событие внимания, из которого получен кандидат
    done: "asyncio.Future | None" = field(default=None, repr=False)   # True: звучала, False: снят без речи
    trace: Any = field(default=None, repr=False)                      # трасса задержки реплики (ТЗ 24, TRC-1)

    @property
    def family(self) -> str:
        return self.source.split(":", 1)[0]

    def resolve(self, spoken: bool) -> None:
        if self.done is not None and not self.done.done():
            self.done.set_result(spoken)


class SpeechQueue:
    def __init__(self, *, speech: Any, play: Callable[["SpeechCandidate"], Awaitable[str | None]],
                 presence: Callable[[], dict], chattiness: Callable[[], str],
                 question_pending: Callable[[], bool] = lambda: False,
                 repeats: Callable[[str], bool] = lambda text: False,
                 on_drop: Callable[["SpeechCandidate", str], None] | None = None,
                 on_played: Callable[["SpeechCandidate"], None] | None = None,
                 clock: Callable[[], float] = time.time):
        """speech - SpeechOutput (is_speaking, stop); play(cand) -> utterance_id: сказать кандидата (ответственность
        компаньона); presence() -> снимок присутствия; chattiness() -> quiet|normal|chatty; question_pending() -> ждут ли
        ответа на вопрос; repeats(text) -> повторяет ли недавно сказанное; on_drop(cand, причина) и on_played(cand)."""
        self.speech, self._play = speech, play
        self._presence, self._chattiness, self._question_pending, self._repeats = presence, chattiness, question_pending, repeats
        self._on_drop, self._on_played = on_drop, on_played
        self._clock = clock
        self.items: list[SpeechCandidate] = []
        self.playing: SpeechCandidate | None = None          # готовится или произносится: от запуска до возврата play()
        self.current: SpeechCandidate | None = None          # последний запущенный (звучит, пока speech.is_speaking())
        self.current_utterance: str | None = None
        self.last_initiative = float("-inf")
        self._run_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._pumping = False
        self.dropped: list[tuple[str, str, str]] = []       # (id, источник, причина): хвост журнала для тестов и отладки

    # ── кандидаты ────────────────────────────────────────────────────────────

    def add(self, cand: SpeechCandidate) -> SpeechCandidate:
        if cand.done is None:
            try:
                cand.done = asyncio.get_running_loop().create_future()
            except RuntimeError:
                pass
        if cand.key:
            for old in [c for c in self.items if c.key == cand.key]:
                self._drop(old, "replaced")                   # одинаковые key: остаётся новейший
        self.items.append(cand)
        while len(self.items) > MAX_QUEUE:
            worst = min(self.items, key=lambda c: (c.priority, -c.created))
            self._drop(worst, "overflow")
        return cand

    def new(self, *, source: str, mode: str, text: str, priority: int, emotion: str | None = None, ttl: float | None = None,
            key: str | None = None, **extra) -> SpeechCandidate:
        """Собрать и положить кандидата; срок жизни по умолчанию из ATT-15."""
        now = self._clock()
        flow_trace.step(None, "speech_queue", "attention", "speech_queue", "speech", size=len(text or ""), label=source.split(":", 1)[0])
        if ttl is None:
            fam = source.split(":", 1)[0]
            ttl = DEFAULT_TTL_S[fam] if fam in ("conversation", "proactive", "question", "announce") else DEFAULT_TTL_S[mode]
        return self.add(SpeechCandidate(new_candidate_id(), source, mode, text, emotion, max(0, min(100, int(priority))),
                                        now, now + ttl, key, **extra))

    def discard(self, cand: SpeechCandidate, reason: str = "cancelled") -> bool:
        if cand in self.items:
            self._drop(cand, reason)
            return True
        return False

    def _drop(self, cand: SpeechCandidate, reason: str) -> None:
        if cand in self.items:
            self.items.remove(cand)
        self.dropped.append((cand.id, cand.source, reason))
        del self.dropped[:-100]
        cand.resolve(False)
        if self._on_drop is not None:
            try:
                self._on_drop(cand, reason)
            except Exception as e:
                logger.warning(f"Снятие кандидата речи: {e}")

    def expire(self) -> list[SpeechCandidate]:
        now = self._clock()
        gone = [c for c in self.items if now > c.expires]
        for c in gone:
            self._drop(c, "expired")
        return gone

    # ── условия ──────────────────────────────────────────────────────────────

    def min_gap(self) -> float:
        return MIN_GAP_S.get(self._chattiness(), MIN_GAP_S["normal"])

    def gap_applies(self, cand: SpeechCandidate) -> bool:
        if cand.family in ("conversation", "question", "announce") or cand.priority >= INTERRUPT_P:
            return False
        return not (cand.family == "module" and cand.mode == "verbatim")

    def self_initiated(self, cand: SpeechCandidate) -> bool:
        return cand.family not in ("conversation", "question")

    def busy(self) -> bool:
        return self.playing is not None or bool(self.speech.is_speaking())

    def blocked_by(self, cand: SpeechCandidate) -> str | None:
        """Причина, по которой кандидата нельзя запускать сейчас (кроме условия 1: оно про занятость вывода), или None."""
        p = self._presence()
        now = self._clock()
        conv = cand.family == "conversation"
        if p.get("user_speaking"):
            return "user_speaking"
        if not conv and now - float(p.get("last_user_ts") or 0.0) < USER_QUIET_S:
            return "user_just_spoke"
        if cand.family not in ("conversation", "question", "announce") and self._question_pending():
            return "question_pending"
        if self.gap_applies(cand) and now - self.last_initiative < self.min_gap() and not self._beats_chatter(cand):
            return "min_gap"
        return None

    def _beats_chatter(self, cand: SpeechCandidate) -> bool:
        """Кандидат D3 и выше, а звучит «болтовня»: он её перебьёт, пауза между репликами ему не помеха (иначе перебивать нечем:
        болтовня сама только что началась)."""
        cur = self.playing or (self.current if self.speech.is_speaking() else None)
        return cur is not None and cand.priority >= D3_P and cur.priority < CHATTER_P

    def best(self) -> SpeechCandidate | None:
        """Лучший из тех, кого можно запускать по условиям 2-4 (повторы при выборе снимаются)."""
        for cand in sorted(self.items, key=lambda c: (-c.priority, c.created)):
            if cand.family not in ("conversation", "question", "announce") and self._repeats(cand.text):
                self._drop(cand, "repeat")                   # ATT-16
                continue
            if self.blocked_by(cand) is None:
                return cand
        return None

    # ── прерывание (ATT-17, ATT-10 D3) ───────────────────────────────────────

    def _should_interrupt(self, new: SpeechCandidate, margin: int) -> bool:
        cur = self.playing or (self.current if self.speech.is_speaking() else None)
        if cur is None:
            return False
        if new.priority >= INTERRUPT_P:
            return cur.priority <= new.priority - margin
        return new.priority >= D3_P and cur.priority < CHATTER_P

    async def _interrupt(self, why: str) -> None:
        cur = self.playing or self.current
        if self.playing is not None and self._run_task is not None and not self._run_task.done():
            self._run_task.cancel()                           # реплика ещё готовится (запрос к модели)
        if self.speech.is_speaking() or self.current_utterance:
            await self.speech.stop(self.current_utterance)
        if cur is not None:
            self.dropped.append((cur.id, cur.source, why))
            del self.dropped[:-100]
            if self._on_drop is not None:
                try:
                    self._on_drop(cur, why)
                except Exception as e:
                    logger.warning(f"Снятие кандидата речи: {e}")

    async def stop_all(self, below: int = INTERRUPT_P) -> int:
        """«Замолчи» (ATT-18): оборвать речь немедленно и очистить очередь от кандидатов ниже `below`. Сколько снято."""
        n = 0
        for cand in [c for c in self.items if c.priority < below]:
            self._drop(cand, "stopped")
            n += 1
        if self.playing is not None and self.playing.priority < below and self._run_task is not None:
            self._run_task.cancel()
        await self.speech.stop(None)
        return n

    # ── планировщик ──────────────────────────────────────────────────────────

    async def pump(self, margin: int = 15) -> SpeechCandidate | None:
        """Один проход: снять устаревшее, при необходимости прервать звучащее и запустить лучшего кандидата.
        Возвращает запущенного кандидата или None. Долгое (подготовка реплики) уходит в фоновую задачу."""
        if self._pumping:
            return None
        self._pumping = True
        try:
            self.expire()
            cand = self.best()
            if cand is None:
                return None
            if self.busy():
                if not self._should_interrupt(cand, margin):
                    return None
                await self._interrupt("interrupted")
                if cand not in self.items:                    # пока останавливали, кандидата успели снять
                    return None
            self.items.remove(cand)
            self.playing = cand
            if self.self_initiated(cand):
                self.last_initiative = self._clock()
            self._run_task = asyncio.get_running_loop().create_task(self._run(cand))
            self._tasks.add(self._run_task)
            self._run_task.add_done_callback(self._tasks.discard)
            return cand
        finally:
            self._pumping = False

    async def _run(self, cand: SpeechCandidate) -> None:
        spoken = False
        try:
            uid = await self._play(cand)
            self.current, self.current_utterance = cand, uid
            spoken = True
        except asyncio.CancelledError:
            pass                                              # прервали: кандидат не возвращается
        except Exception as e:
            logger.warning(f"Реплика не прозвучала ({cand.source}): {e}")
        finally:
            if self.playing is cand:
                self.playing = None
            cand.resolve(spoken)
            if self._on_played is not None:
                try:
                    self._on_played(cand)
                except Exception as e:
                    logger.warning(f"После реплики: {e}")

    async def idle(self) -> None:
        """Дождаться фоновых задач реплик (для тестов и остановки)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def snapshot(self) -> list[dict]:
        """Очередь для дашборда и отладки."""
        now = self._clock()
        return [{"id": c.id, "source": c.source, "mode": c.mode, "priority": c.priority, "text": c.text[:120],
                 "age_s": round(now - c.created, 1), "ttl_s": round(c.expires - now, 1)}
                for c in sorted(self.items, key=lambda c: (-c.priority, c.created))]
