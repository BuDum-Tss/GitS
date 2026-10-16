"""Режим сна как фоновая политика модуля памяти (ТЗ 27, EPI-4; ТЗ 18).

Модуль памяти (в ядре) объявляет менеджеру фокуса политику `sleep` (`domain.episodes.SLEEP_POLICY`: фон, прерываемая) и показывает
статус `active_policy = sleep`, пока идёт сон. Сон запускается, когда:
  - простой дольше `idle_min` минут (`idle`),
  - наступил час расписания (`scheduled`),
  - давно не спала и простоя нет (`catchup`, малый бюджет токенов).
Возвращение пользователя (ввод с клавиатуры или сообщение), занятость в разговоре или переключение фокуса прерывают сон: ставится флаг,
конвейер останавливается на ближайшей безопасной точке между эпизодами, а если за `grace_s` секунд не успел, задача отменяется
(запрос к LM Studio уже идущую генерацию не отменит, но ядро её не ждёт; эпизод остаётся в прежнем статусе).
Опрос раз в `poll_s` секунд только проверяет условия; речи по таймеру здесь нет."""
import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from app.sleep import SleepRunner, episodes_cfg
from domain import episodes as dom

logger = logging.getLogger("episodes")

WAKE_IDLE_S = 3.0              # ввод моложе этого возраста значит «пользователь вернулся»


class SleepScheduler:
    def __init__(self, runner: SleepRunner, *, store, settings: Callable[[], dict], clock: Callable[[], float] = time.time,
                 idle_seconds: Callable[[], float] = lambda: 0.0, last_user_ts: Callable[[], float] = lambda: 0.0,
                 busy: Callable[[], bool] = lambda: False, grace_s: float = 3.0, poll_s: float = 30.0, watch_s: float = 1.0):
        """idle_seconds() - простой ввода (модуль desktop); last_user_ts() - время последней реплики пользователя; busy() -
        идёт разговор; store - хранилище эпизодов (отметка `last_sleep`)."""
        self.runner, self.store, self.settings, self.clock = runner, store, settings, clock
        self.idle_seconds, self.last_user_ts, self.busy = idle_seconds, last_user_ts, busy
        self.grace_s, self.poll_s, self.watch_s = grace_s, poll_s, watch_s
        self.state = "awake"
        self.on_state: Callable[[str], None] | None = None     # сон начался или кончился (ТЗ 32: среда sleep)
        self.plan: dom.SleepPlan | None = None
        self.last_report: dict | None = None
        self.interrupted_after_s: float | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._loop_task: asyncio.Task | None = None
        self._asked_at: float | None = None
        self._started_at = 0.0

    # ── политика для менеджера фокуса ────────────────────────────────────────

    @staticmethod
    def policies() -> dict:
        """Манифест политик модуля памяти для FocusManager.policies_of."""
        return {"default_policy": None, "policies": [dict(dom.SLEEP_POLICY)]}

    def focus_status(self) -> dict:
        """Статус для FocusManager.status_of: сон идёт, фокус не занят, прерывать можно."""
        return {"active_policy": dom.SLEEP_POLICY["id"] if self.state == "sleeping" else None, "interruptible": True,
                "blocked": False}

    # ── решение ──────────────────────────────────────────────────────────────

    def _idle(self) -> float:
        since_user = self.clock() - self.last_user_ts() if self.last_user_ts() else float("inf")
        return min(float(self.idle_seconds() or 0.0), since_user)

    def decide(self) -> dom.SleepPlan | None:
        now = self.clock()
        last = self.store.get_meta("last_sleep", 0.0) or 0.0
        return dom.decide_sleep(idle_s=self._idle(), now=now, hour=datetime.fromtimestamp(now).hour, last_sleep=last,
                                cfg=episodes_cfg(self.settings))

    async def tick(self) -> bool:
        """Проверить условия и начать сон. True, если сон начат."""
        if self._task is not None and not self._task.done():
            return False
        if self.busy():
            return False
        plan = self.decide()
        if plan is None:
            return False
        self.start(plan)
        return True

    # ── ход сна ──────────────────────────────────────────────────────────────

    def start(self, plan: dom.SleepPlan) -> asyncio.Task:
        self._stop = asyncio.Event()
        self._asked_at = None
        self.plan, self.state, self._started_at = plan, "sleeping", self.clock()
        self._notify_state()
        self._task = asyncio.create_task(self._run(plan))
        return self._task

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def interrupt(self, reason: str = "user") -> None:
        """Прервать сон: флаг для безопасной точки и отмена через `grace_s`, если конвейер не остановился сам."""
        if self._task is None or self._task.done() or self._stop.is_set():
            return
        self._stop.set()
        self._asked_at = self.clock()
        logger.info(f"Сон прерывается: {reason}")
        task = self._task
        asyncio.get_running_loop().call_later(self.grace_s, lambda: task.cancel() if not task.done() else None)

    async def _watch(self) -> None:
        """Пока идёт сон, следит за возвращением пользователя и занятостью."""
        while True:
            await asyncio.sleep(self.watch_s)
            if 0.0 < float(self.idle_seconds() or 0.0) < WAKE_IDLE_S:          # 0 - модуль desktop не прислал простой
                self.interrupt("ввод")
            elif self.last_user_ts() > self._started_at:
                self.interrupt("сообщение")
            elif self.busy():
                self.interrupt("разговор")

    async def _run(self, plan: dom.SleepPlan) -> None:
        watcher = asyncio.create_task(self._watch())
        try:
            self.last_report = await self.runner.run(plan, self.should_stop)
        except asyncio.CancelledError:
            self.last_report = {"mode": plan.mode, "interrupted": True, "cancelled": True}
        except Exception as e:
            logger.warning(f"Сон: {e}")
            self.last_report = {"mode": plan.mode, "error": str(e)[:200]}
        finally:
            watcher.cancel()
            if self._asked_at is not None:
                self.interrupted_after_s = round(self.clock() - self._asked_at, 3)
            self.state = "awake"
            self._notify_state()

    def _notify_state(self) -> None:
        if self.on_state is not None:
            try:
                self.on_state(self.state)
            except Exception as e:
                logger.warning(f"Смена состояния сна: {e}")

    # ── жизненный цикл ───────────────────────────────────────────────────────

    async def start_loop(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_s)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Планировщик сна: {e}")

    async def stop(self) -> None:
        for t in (self._loop_task, self._task):
            if t is not None and not t.done():
                t.cancel()
        for t in (self._loop_task, self._task):
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._loop_task = self._task = None

    def status(self) -> dict:
        counts = self.store.counts()
        return {"state": self.state, "mode": self.plan.mode if self.plan and self.state == "sleeping" else None,
                "last_sleep": self.store.get_meta("last_sleep", 0.0), "episodes": counts, "last_report": self.last_report,
                "interrupted_after_s": self.interrupted_after_s}
