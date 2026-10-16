"""PlanNudger (ТЗ 30, хвост): напоминания о планах пользователя идут импульсами, а не опросом «стоит ли заговорить».

Раз в час (и при старте) без обращения к модели забирает из памяти планы, о которых пора напомнить (`plans_to_nudge`: срок в
ближайшие сутки, за последние 12 часов не напоминали), и превращает каждый в напоминание ядра себе (`ImpulseTimer.add`) за
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


class PlanNudger:
    def __init__(self, memory: Any, add: Callable[..., Awaitable[dict]], *, due_of: Callable[[dict], float | None],
                 clock: Callable[[], float] = time.time, lead_s: float = LEAD_S, every_s: float = SWEEP_S, tasks: Any = None):
        """memory - MemoryReader (plans_to_nudge), add - `ImpulseTimer.add`, due_of - срок плана (эпоха) или None,
        tasks - `TaskService` (ТЗ 32): план становится задачей `plan` со сроком, напоминание берёт срок оттуда."""
        self.memory, self.add, self.due_of, self.clock, self.tasks = memory, add, due_of, clock, tasks
        self.lead_s, self.every_s = lead_s, every_s
        self._task: asyncio.Task | None = None

    async def sync(self) -> int:
        """Поставить напоминания по планам, которым пора. Возвращает их число."""
        plans = await asyncio.to_thread(self.memory.plans_to_nudge)
        now = self.clock()
        for plan in plans:
            due = self.due_of(plan) or now
            if self.tasks is not None and plan.get("id"):
                try:
                    due = (await self.tasks.upsert_plan(str(plan["id"]), plan["text"], due)).due or due
                except Exception as e:                       # задачи не должны ломать напоминание
                    logger.warning(f"План как задача: {e}")
            await self.add(max(now, due - self.lead_s), f"У пользователя скоро запланировано: {plan['text']}",
                           source={"kind": "plan", "plan_id": plan.get("id")}, untrusted=True)
        return len(plans)

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
