"""Эмоция персонажа: оркестрация (ТЗ 26). Чистая логика в `domain/affect.py`, часы и время жизни подставляются, поэтому в
тестах время управляемое.

Эмоцию выбирает классификатор (GLiNER) по обращению пользователя или событию; `fast_brain.publish` зовёт `set`. Новая
эмоция перебивает старую и держится `emotion_ttl_s` секунд (настройка), потом возвращается `idle`. Наружу уходят только
факт для контекста реплики (`emotion_block`) и подпись для питомца (`presence`). Эмоция не меняет факты, конституцию,
разрешения и безопасность. На диск ничего не пишется: за секунды нечего сохранять.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from domain import affect as dom

log = logging.getLogger("affect")


class AffectService:
    def __init__(self, ttl: Callable[[], float] | None = None, *, clock: Callable[[], float] = time.time,
                 on_change: Callable[[dict], None] | None = None):
        self._ttl = ttl
        self.clock = clock
        self.on_change = on_change              # поля presence; синхронный
        self.state = dom.State()
        self._timer: asyncio.TimerHandle | None = None

    @property
    def ttl(self) -> float:
        """Сколько секунд держится эмоция (настройка читается при каждом обращении, меняется на лету)."""
        try:
            return dom.clamp_ttl(self._ttl()) if self._ttl is not None else dom.DEFAULT_TTL_S
        except Exception:
            return dom.DEFAULT_TTL_S

    def set(self, label, reason: str = "") -> None:
        """Новая эмоция перебивает старую. Не из набора и idle игнорируются. Не бросает наружу."""
        try:
            new = dom.set_emotion(self.state, label, self.clock(), reason)
            if new is self.state:
                return
            self.state = new
            self._arm()
        except Exception as e:
            log.warning("эмоция: %s", e)

    def _arm(self) -> None:
        """По истечении срока в presence уходит idle (питомец возвращается к спокойной картинке)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                                  # вне цикла событий (тесты): истечение видно по current()
        self._timer = loop.call_later(self.ttl, self._expire, self.state.ts)

    def _expire(self, ts: float) -> None:
        if self.state.ts != ts or self.on_change is None:
            return
        try:
            self.on_change({"emotion": dom.NEUTRAL, "emotion_ts": self.clock()})
        except Exception as e:
            log.warning("рассылка эмоции: %s", e)

    # ── выдача ──────────────────────────────────────────────────────────────

    def current(self) -> str:
        return dom.current(self.state, self.ttl, self.clock())

    def emotion_block(self) -> str:
        return dom.emotion_fact(self.state, self.ttl, self.clock())

    def snapshot(self) -> dict:
        label = self.current()
        return {"emotion": label, "reason": self.state.reason if label != dom.NEUTRAL else "", "ttl_s": self.ttl,
                "ts": self.state.ts}
