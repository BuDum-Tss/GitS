"""Решения политик без игрового вызова (ТЗ 11e, POL-4, POL-11, POL-12): ступени скорости, срез боя и его бюджет, застрявшие
задачи и принудительная работа с откатом, выбор модели хода, условие запуска игры. Всё здесь чистые функции или маленькие
объекты с часами, которые подставляют тесты. Команды в игру отправляет контроллер."""
from __future__ import annotations

import time
from collections import deque
from typing import Callable

TICKS_PER_HOUR = 2500


# ── observe: ступени скорости ────────────────────────────────────────────────────────────────

def allowed_speeds(ladder: list[int], max_speed: int) -> list[int]:
    out = [s for s in ladder if 1 <= s <= max_speed]
    return out or [1]


def next_speed(current: int, ladder: list[int], max_speed: int) -> int:
    speeds = allowed_speeds(ladder, max_speed)
    if current not in speeds:
        return speeds[0]
    i = speeds.index(current)
    return speeds[min(i + 1, len(speeds) - 1)]


def observe_speed_plan(*, now: float, quiet_since: float, last_step_at: float, current_speed: int, ladder: list[int],
                       max_speed: int, step_quiet_s: float) -> int | None:
    """Новая скорость или None (не менять). Скорость растёт, когда тихо step_quiet_s и с прошлой ступени тоже прошло столько."""
    if now - quiet_since < step_quiet_s or now - last_step_at < step_quiet_s:
        return None
    nxt = next_speed(current_speed, ladder, max_speed)
    return nxt if nxt != current_speed else None


# ── combat: срез и бюджет ────────────────────────────────────────────────────────────────────

def combat_ready(*, tick: int | None, last_turn_tick: int | None, slice_ticks: int, now: float, last_turn_at: float,
                 fallback_s: float = 30.0) -> bool:
    """Пора ли новому ходу боя: прошёл срез игровых тиков (игра шла) или fallback_s по часам (игра стояла на паузе)."""
    if last_turn_tick is None or tick is None:
        return now - last_turn_at >= fallback_s
    if tick - last_turn_tick >= slice_ticks:
        return True
    return now - last_turn_at >= fallback_s


class FightBudget:
    """Бюджет боя: не больше max_turns ходов за один бой и max_per_min за минуту (ТЗ 11e, раздел 3.3)."""

    def __init__(self, max_turns: int = 30, max_per_min: int = 4, clock: Callable[[], float] = time.time):
        self.max_turns = max_turns
        self.max_per_min = max_per_min
        self._clock = clock
        self.turns = 0
        self._stamps: deque = deque()

    def reset(self) -> None:
        self.turns = 0
        self._stamps.clear()

    def allow(self) -> tuple[bool, str]:
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= 60:
            self._stamps.popleft()
        if self.turns >= self.max_turns:
            return False, f"бюджет боя исчерпан: {self.turns} ходов из {self.max_turns}"
        if len(self._stamps) >= self.max_per_min:
            return False, f"слишком часто: {self.max_per_min} ходов в минуту"
        return True, ""

    def spend(self) -> None:
        self.turns += 1
        self._stamps.append(self._clock())


# ── выбор модели хода ─────────────────────────────────────────────────────────────────────────

def use_local_model(model: str, turn_index: int, claude_every: int = 3, claude_blocked: bool = False) -> bool:
    """True, если ход ведёт локальная модель. mixed: Claude на первом ходе боя и затем каждый claude_every-й."""
    if claude_blocked or model == "local":
        return True
    if model == "claude":
        return False
    if model == "mixed":
        return turn_index % max(1, claude_every) != 0
    return True


# ── manual: застрявшие задачи и принуждение ───────────────────────────────────────────────────

def stalls(tasks: list[dict], first_seen: dict[str, int], tick: int | None, stall_hours: float) -> list[dict]:
    """Задачи, которые не двигаются дольше stall_hours игровых часов. first_seen (описание -> тик первого появления)
    обновляется: новые задачи запоминаются, выполненные забываются."""
    out: list[dict] = []
    live = set()
    for t in tasks:
        desc = str(t.get("description") or "")
        # шаги, которые ждут исследование или заблокированы: не принуждаем (исследования, research_unlock)
        if not desc or t.get("status") in ("done", "waiting_research", "blocked"):
            continue
        live.add(desc)
        if tick is None:
            continue
        first = first_seen.setdefault(desc, tick)
        hours = (tick - first) / TICKS_PER_HOUR
        if hours >= stall_hours:
            out.append(t)
    for desc in [d for d in first_seen if d not in live]:
        del first_seen[desc]
    return out


def has_target(task: dict) -> bool:
    return isinstance(task.get("x"), int) and isinstance(task.get("z"), int)


def plan_forced(stalled: list[dict], *, forced: dict, idle_pawn_ids: list[str], tick: int | None, interval_hours: float,
                confirm: bool) -> list[dict]:
    """Действия по застрявшим задачам: {type: force|note, ...}. Не чаще одного принуждения на задачу за interval_hours.
    confirm=True: только предложение владельцу (note), без команды."""
    actions: list[dict] = []
    for t in stalled:
        desc = str(t.get("description"))
        if not has_target(t):
            actions.append({"type": "note", "task": desc, "text": "нет клетки: принудить работу нельзя, нужна клетка (x, z)"})
            continue
        last = forced.get(desc)
        if last and tick is not None and (tick - last["tick"]) / TICKS_PER_HOUR < interval_hours:
            continue
        pawn = str(t.get("pawn_id") or "") or next((p for p in idle_pawn_ids if p), "")
        if not pawn:
            actions.append({"type": "note", "task": desc, "text": "некого назначить: нет свободного поселенца"})
            continue
        work = str(t.get("work") or "")
        cmd = {"type": "force_work", "pawn_id": pawn, "targets": [{"x": t["x"], "z": t["z"]}], "queue": False, "work": work}
        if confirm:
            actions.append({"type": "note", "task": desc, "text": f"предлагаю принудить {pawn} к работе на ({t['x']},{t['z']}); "
                                                                  "нужно подтверждение владельца"})
        else:
            actions.append({"type": "force", "task": desc, "pawn": pawn, "cmd": cmd})
    return actions


def plan_rollback(forced: dict, done: set[str], tick: int | None, rollback_hours: float) -> list[dict]:
    """Откат принуждения: задача сделана (запись снимается) или не сдвинулась за rollback_hours (очередь пешки сбрасывается)."""
    actions: list[dict] = []
    for desc, rec in list(forced.items()):
        if desc in done:
            actions.append({"type": "forget", "task": desc})
            continue
        if tick is not None and (tick - rec["tick"]) / TICKS_PER_HOUR >= rollback_hours:
            actions.append({"type": "rollback", "task": desc, "pawn": rec["pawn"],
                            "cmd": {"type": "clear_work_queue", "pawn_id": rec["pawn"], "stop_current": False}})
    return actions


# ── запуск игры (POL-12) ──────────────────────────────────────────────────────────────────────

def launch_allowed(*, in_focus: bool, autostart: bool, seen_running_in_focus: bool) -> bool:
    """Модуль сам запускает игру только в фокусе: при включённом autostart_game (по умолчанию выключен) или чтобы
    поднять игру, которая упала при фокусе модуля. Явная просьба (инструмент launch_game) проверяется отдельно."""
    return in_focus and (autostart or seen_running_in_focus)
