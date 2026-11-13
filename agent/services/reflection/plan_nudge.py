"""PlanNudger (ТЗ 30, хвост): напоминания о планах пользователя идут импульсами, а не опросом «стоит ли заговорить».

Планы это задачи `plan` в `tasks.db` (ТЗ 32). Раз в час (и при старте) без обращения к модели забирает те, о которых пора напомнить
(срок в ближайшие сутки, за последние 12 часов не напоминали), и превращает каждый в напоминание ядра себе (`ImpulseTimer.add`) за
`lead_s` до срока. Дальше всё как у остальных импульсов: срок -> событие `self.reminder` -> размышление решает, сказать ли.
Текст напоминания это факт, не реплика; текст плана пришёл от пользователя или из чужих сообщений, поэтому стимул считается
недоверенным (действия `do` по нему не исполняются, сказать можно).
"""
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("impulses")

LEAD_S = 30 * 60.0          # напомнить за полчаса до срока
SWEEP_S = 3600.0            # как часто просматривать планы (без модели)
LOOKAHEAD_S = 24 * 3600.0   # планы со сроком дальше суток пока не трогаем
REPEAT_S = 12 * 3600.0      # не напоминать о том же плане чаще


class PlanNudger:
    def __init__(self, tasks: Any, add: Callable[..., Awaitable[dict]], *, clock: Callable[[], float] = time.time,
                 lead_s: float = LEAD_S, every_s: float = SWEEP_S):
        """tasks - `TaskService` (планы `plan` со сроком), add - `ImpulseTimer.add`."""
        self.tasks, self.add, self.clock = tasks, add, clock
        self.lead_s, self.every_s = lead_s, every_s
        self._task: asyncio.Task | None = None

    async def sync(self) -> int:
        """Поставить напоминания по планам, которым пора. Возвращает их число."""
        if self.tasks is None:
            return 0
        now = self.clock()
        count = 0
        for plan in await self.tasks.due_for_nudge(LOOKAHEAD_S):
            if now - float(plan.meta.get("nudged") or 0) < REPEAT_S:
                continue
            await self.add(max(now, (plan.due or now) - self.lead_s), f"У пользователя скоро запланировано: {plan.goal}",
                           source={"kind": "plan", "plan_id": plan.id}, untrusted=True)
            try:
                await self.tasks.mark_nudged(plan.id)
            except Exception as e:
                logger.warning(f"Отметка напоминания о плане: {e}")
            count += 1
        return count

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Напоминания о планах: {e}")
            await asyncio.sleep(self.every_s)
