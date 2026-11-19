"""Наблюдатель целей развития: когда агенту пора заметить, что цели нет, она закрыта или стоит без действий.

Агент ставит цели сам (вводная `game_brief`, задачи ядра `assistant.create_task` / `close_task`). У модуля прямого признака «цель
закрыта» нет, но ядро отдаёт модулю снимок открытых задач среды (`core.tasks`, см. `core_tasks`): активной нет в снимке значит,
цели нет (или её только что закрыли: `core_tasks.closed_goal`). Третий признак наблюдаемый: цель есть, а агент не вызывал инструменты
дольше `goal_review_idle_s`. По любому из них модуль шлёт событие `rimworld.goal_review` с ФАКТАМИ (пробелы из `colony_assess`), без
приказов: что делать, решает агент.

Не шлём: ядро ещё не отдало снимок задач, игра не в фокусе (проверяет контроллер), идёт бой или угроза, пауза агента, агент работает
прямо сейчас (мог ставить цель, снимок ядра обновляется раз в 10 с); чаще раза в `min_s`. Состояние «цели нет» должно продержаться
`GRACE_S`, чтобы агент успел поставить новую после закрытия старой.
"""
GRACE_S = 20.0           # «цели нет» держится столько секунд подряд, прежде чем шлём событие
ACTIVE_S = 20.0          # агент вызывал инструмент не дальше этого: он занят (возможно, ставит цель), событие не шлём
MIN_S = 240.0            # не чаще раза в столько секунд (настройка goal_review_min_s)
IDLE_S = 600.0           # цель есть, а действий агента не было столько секунд (настройка goal_review_idle_s)


def _minutes(seconds: float) -> str:
    return f"{round(seconds / 60)} мин" if seconds >= 90 else f"{max(1, round(seconds))} с"


class GoalWatch:
    def __init__(self, min_s: float = MIN_S, idle_s: float = IDLE_S):
        self.min_s = min_s
        self.idle_s = idle_s
        self._last_emit: float | None = None
        self._none_since: float | None = None        # с какого момента цели нет подряд
        self._seen_goal: str | None = None           # id активной цели, которую видели последней
        self._goal_since = 0.0                       # когда впервые увидели эту цель (отсчёт «без действий», если инструментов ещё не было)

    def reset(self) -> None:
        self._none_since = None
        self._seen_goal = None

    def update(self, now: float, *, known: bool, goal: dict | None, closed: str = "", danger: bool = False, agent_paused: bool = False,
               last_tool_at: float | None = None, min_s: float | None = None, idle_s: float | None = None) -> dict | None:
        """Один тик. `goal`: активная цель `{id, title, done, total}` или None. Возвращает `{"reason": "closed" | "none" | "stalled",
        "goal": название активной цели, "closed": название закрытой, "idle_s": сколько без действий}` или None."""
        min_s = self.min_s if min_s is None else min_s
        idle_s = self.idle_s if idle_s is None else idle_s
        if not known or danger or agent_paused:
            self._none_since = None                  # бой и пауза рвут «подряд»; снимка нет: о цели ничего не известно
            return None
        busy = last_tool_at is not None and now - last_tool_at < ACTIVE_S
        if goal is None:
            self._seen_goal = None
            if self._none_since is None:
                self._none_since = now
            if busy or now - self._none_since < GRACE_S:
                return None
            reason, quiet = ("closed" if closed else "none"), 0.0
        else:
            self._none_since = None
            if self._seen_goal != goal.get("id"):
                self._seen_goal, self._goal_since = goal.get("id"), now
            quiet = now - max(last_tool_at or 0.0, self._goal_since)
            if quiet < idle_s:
                return None
            reason = "stalled"
        if self._last_emit is not None and now - self._last_emit < min_s:
            return None
        self._last_emit = now
        return {"reason": reason, "goal": str((goal or {}).get("title") or ""), "closed": closed if reason == "closed" else "",
                "idle_s": round(quiet), "done": int((goal or {}).get("done") or 0), "total": int((goal or {}).get("total") or 0)}


def review_text(res: dict, gaps: str) -> str:
    """Текст события: только факты (какая цель, что с ней, что в колонии), без приказов; решение о цели за агентом."""
    if res["reason"] == "closed":
        head = f"Цель «{res['closed']}» закрыта, активной цели нет."
    elif res["reason"] == "stalled":
        head = (f"Активная цель «{res['goal']}» (шагов выполнено {res['done']} из {res['total']}), "
                f"действий не было {_minutes(res['idle_s'])}.")
    else:
        head = "Активной цели нет."
    return f"{head} Состояние: {gaps}. Оценка колонии доступна инструментом colony_assess."
