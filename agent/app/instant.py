"""Мгновенный уровень (ТЗ 25, FAST-3): пока идёт долгая обработка, пользователь не остаётся в тишине.

На маршруте `task` через `ack_ms` (по умолчанию 300 мс) без реплики уходит факт «принято, начинаю»; при затянутом ответе (любой
маршрут) через `filler_ms` без реплики уходит факт-филлер. Код передаёт только факт, реплику формулирует модель
(`ReplyGenerator.quick_fact`, вызов мимо блокировки модели, чтобы не ждать план, который подтверждение и должно опередить).
Эмоция и жест «задумалась» ставятся при входе обращения (`fast_brain.publish`). Подтверждение и филлер не заменяют ответ и
не считаются им; настоящая реплика отменяет ещё не сработавшие таймеры.
"""
import asyncio
import contextvars
import logging

logger = logging.getLogger("companion")

ACK_FACT = ("Пользователь только что дал тебе поручение, и ты берёшься за него прямо сейчас; результата пока нет. "
            "Скажи одной короткой фразой в своём характере, что принимаешь и начинаешь. Ничего не выдумывай про результат.")
FILLER_FACT = ("Ты готовишь ответ дольше обычного, а пользователь уже ждёт. Скажи одной короткой фразой в своём характере, "
               "что ещё думаешь над этим. Ответ не выдумывай и не обещай сроки.")

current_turn: contextvars.ContextVar = contextvars.ContextVar("instant_turn", default=None)


class Turn:
    """Один ход обращения: счётчики реплик (настоящих и мгновенного уровня) и таймеры."""

    def __init__(self):
        self.speech_events = 0       # любые реплики хода
        self.quick_events = 0        # из них подтверждение и филлер
        self.acked = False
        self.ended = False
        self._tasks: list[asyncio.Task] = []

    @property
    def spoke(self) -> bool:
        """Прозвучала настоящая реплика (подтверждение и филлер не считаются)."""
        return self.speech_events > self.quick_events

    def end(self) -> None:
        self.ended = True
        if current_turn.get() is self:
            current_turn.set(None)
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()


def note_speech() -> None:
    """Реплика хода прозвучала (зовёт Companion при каждой реплике): настоящая отменяет ещё не сработавшие таймеры."""
    turn = current_turn.get()
    if turn is not None:
        turn.speech_events += 1


class InstantLevel:
    def __init__(self, host):
        self._h = host

    def _cfg(self) -> dict:
        try:
            cfg = self._h._settings.load().get("fast_contour")
        except Exception:
            cfg = None
        return cfg if isinstance(cfg, dict) else {}

    def begin(self, text: str, *, task: bool) -> Turn:
        """Начать ход: таймер подтверждения (только `task`) и таймер филлера. Закончить: `turn.end()`."""
        cfg = self._cfg()
        turn = Turn()
        current_turn.set(turn)
        ack_ms, filler_ms = cfg.get("ack_ms", 300), cfg.get("filler_ms", 4000)
        if task and ack_ms and ack_ms > 0:
            turn._tasks.append(asyncio.ensure_future(self._after(turn, ack_ms / 1000, ACK_FACT, text, ack=True)))
        if filler_ms and filler_ms > 0:
            turn._tasks.append(asyncio.ensure_future(self._after(turn, filler_ms / 1000, FILLER_FACT, text, ack=False)))
        return turn

    async def _after(self, turn: Turn, delay_s: float, fact: str, text: str, *, ack: bool) -> None:
        try:
            await asyncio.sleep(delay_s)
            if turn.ended or turn.spoke or (not ack and turn.acked):
                return                                  # ответ уже идёт, либо «принято» уже сказано: филлер лишний
            reply = await self._h.reply_generator.quick_fact(fact, text)
            if not reply or turn.ended or turn.spoke:
                return                                  # пока формулировали, пошёл настоящий ответ
            if ack:
                turn.acked = True
            turn.quick_events += 1                      # реплику посчитает note_speech: настоящей она от этого не станет
            await self._h.say(reply, direct=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info(f"Мгновенный уровень не сработал: {e}")
