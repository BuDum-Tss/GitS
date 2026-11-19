"""WorkLoop (ТЗ 33): автономная работа единого агента в среде контентного модуля.

Пока агент в среде модуля, который объявил `workspace.autonomous` (игра, мир), и он свободен, раз в `work_interval_s` ядро шлёт ему
внутреннее событие `self.work_tick`. Это обычный самоимпульс: он идёт тем же конвейером внимания, маршрутом `think`, агент решает сам,
что делать (посмотреть узел и запросы, продвинуть задачу, поставить себе напоминание) или промолчать. Текст события это факт, не
команда. Цикл молчит, когда агент занят, пользователь просит не отвлекать, выключена настройка `autoplay` или кончился лимит подписки.
Каждый модуль задаёт свой темп (`work_interval_s`), общего таймера «играй» в ядре нет.
При внимании v2 (`GITS_ATTENTION=v2`) тот же тик приходит событием `core.work_tick` (`emit` подменяет ядро), а «занята» дополняет `busy()`.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

from kernel.event import KIND_WORK_TICK

logger = logging.getLogger("work_loop")

DEFAULT_INTERVAL_S = 120
CHECK_S = 5.0
NO_MODULE_ENVS = ("assistant", "sleep")
TASK_WORKSPACES = {"montage": 30}


class WorkLoop:
    def __init__(self, *, env_of: Callable[[], str | None], manifest_of: Callable[[str], dict | None], agent,
                 emit: Callable[[dict], Awaitable[None]], settings: Callable[[], dict], quiet: Callable[[], bool],
                 blocked: Callable[[], bool] = lambda: False, clock: Callable[[], float] = time.time, tasks=None,
                 busy: Callable[[], bool] = lambda: False):
        """env_of() - текущая среда; manifest_of(id) - манифест модуля; agent - единый агент (`busy`, `last_end`);
        emit(тело события) - `Attention.submit_self` (при внимании v2 переводит тик в событие `core.work_tick`); settings() - настройки ядра;
        quiet() - «не отвлекать»; blocked() - лимит подписки; busy() - дополнительный признак «занята» (при v2: идёт ход по пачке
        событий или тик уже ждёт в очереди; `agent.busy` его не видит между запуском хода и входом в блокировку)."""
        self._env_of, self._manifest_of, self._agent, self._emit = env_of, manifest_of, agent, emit
        self._settings, self._quiet, self._blocked, self._clock = settings, quiet, blocked, clock
        self._busy = busy
        self._tasks = tasks                      # TaskService: цель среды (`workspace.default_goal`) и активная задача
        self._last = 0.0                         # когда в последний раз выдан тик (или вход в среду)
        self._since: dict[str, float] = {}       # когда агент вошёл в среду: тик не раньше интервала после входа
        self._task: asyncio.Task | None = None

    def spec(self) -> tuple[str, dict] | None:
        """(id модуля, объявление среды) для текущей автономной среды или None."""
        module = self._env_of()
        if not module or module in NO_MODULE_ENVS:
            return None
        ws = (self._manifest_of(module) or {}).get("workspace") or {}
        return (module, ws) if ws.get("autonomous") or module in TASK_WORKSPACES else None

    async def tick(self) -> bool:
        """Один шаг: выдать событие, если пора. Возвращает, выдано ли оно."""
        target = self.spec()
        now = self._clock()
        if target is None:
            self._since.clear()
            return False
        module, ws = target
        self._since.setdefault(module, now)
        task_interval = TASK_WORKSPACES.get(module)
        if ((task_interval is None and not self._settings().get("autoplay", False))
                or self._quiet() or self._blocked() or self._agent.busy or self._busy()):
            return False
        interval = float(task_interval or ws.get("work_interval_s") or DEFAULT_INTERVAL_S)
        ready = max(self._last, getattr(self._agent, "last_end", 0.0), self._since[module])
        if now - ready < interval:
            return False
        if task_interval is not None:
            from services.tasks import model as dom
            task = await self._tasks.active(module) if self._tasks is not None else None
            if task is None or task.status != dom.ACTIVE or self._env_of() != module or self._agent.busy or self._busy():
                return False
            facts = self._describe_task(task)
        else:
            facts = await self._task_facts(module, ws)
        self._last = now
        title = ws.get("title") or module
        purpose = ws.get("purpose")
        text = (f"Рабочий цикл: ты в среде «{title}» и свободна. " + (f"Назначение среды: {purpose}. " if purpose else "")
                + facts + "Событий и просьб сейчас нет.")
        await self._emit({"kind": KIND_WORK_TICK, "text": text, "urgency_hint": 1, "data": {"module": module}})
        return True

    async def _task_facts(self, module: str, ws: dict) -> str:
        """Факты о задачах среды. Нет открытой задачи, а модуль объявил `default_goal`: ядро ставит её само (origin self) и
        просит Planner разбить на шаги, чтобы у агента всегда была цель и следующий шаг. Исключение: среда с `self_goals: true`
        (цели развития ставит сам агент по фактам среды): ядро задачу не создаёт, тик говорит только, есть ли открытая задача, а
        после закрытия цели агент не получает механически ту же самую."""
        svc = self._tasks
        if svc is None:
            return ""
        try:
            task = await svc.active(module)
            goal = str(ws.get("default_goal") or "").strip()
            if task is None and goal and not ws.get("self_goals"):
                from services.tasks import model as dom
                task = await svc.create(goal, workspace=module, origin=dom.SELF, plan=True)
                task = await svc.activate(task.id)
            if task is None:
                return "Открытых задач у среды нет. "
            return self._describe_task(task)
        except Exception as e:
            logger.warning(f"Задача среды: {e}")
            return ""

    @staticmethod
    def _describe_task(task) -> str:
        from services.tasks import model as dom
        step = dom.next_step(task)
        done, total = dom.progress(task)
        line = f"Активная задача: «{task.goal}» (шагов выполнено {done} из {total}). "
        if step is not None:
            line += f"Следующий шаг: {step.text}. "
        return line

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"Рабочий цикл: {e}")
            await asyncio.sleep(CHECK_S)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
