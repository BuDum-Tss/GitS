"""Автоскорость игры: в спокойное время держать максимальную скорость, при угрозе снижать до обычной.

`plan_speed` чистая: по состоянию мода решает, какую скорость поставить (или None: ничего не делать). Применяет её controller
(команда мода `set_time_speed`). Скорости мода: 0 пауза, 1 обычная, 2 быстрая, 3 очень быстрая, 4 максимальная.
"""
import statediff

MODES = ("off", "max")
DEFAULT_MODE = "max"
MAX_SPEED = 4
RAISE_COOLDOWN_S = 20      # не чаще раза в столько секунд повышаем скорость (игра сама сбрасывает её на событиях)
AUTO_UNPAUSE_S = 40        # забытая пауза: снимаем, когда она идёт подряд не меньше стольких секунд (настройка auto_unpause_s, 0 = выкл.)
CALM_RETURN_S = 30         # после угрозы или боя максимум возвращаем, когда спокойно не меньше этого


def normalize(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def is_danger(state: dict) -> bool:
    """Угроза на карте, мобилизованный колонист или бой."""
    if state.get("threats"):
        return True
    for p in (state.get("colony") or {}).get("pawns") or []:
        if p.get("is_drafted") or p.get("current_job") in statediff._FIGHT_JOBS:
            return True
    return False


def plan_speed(state: dict, mode: str, now: float, last_set: float, paused_by_planning: bool = False,
               danger_at: float | None = None, paused_since: float | None = None, unpause_after_s: float = AUTO_UNPAUSE_S) -> int | None:
    """Какую скорость поставить сейчас. `last_set` время прошлой нашей команды; `danger_at` когда в последний раз была опасность
    (None: не было). Паузу планирования не трогаем. Обычную паузу снимаем только как забытую: она идёт подряд (`paused_since`)
    не меньше `unpause_after_s` (0: не снимать), угрозы нет, игра в колонии. Команда `set_time_speed` с ненулевой скоростью
    сама выводит игру из паузы (мод выставляет CurTimeSpeed напрямую)."""
    if normalize(mode) != "max" or paused_by_planning:
        return None
    if state.get("paused"):
        if (unpause_after_s <= 0 or paused_since is None or now - paused_since < unpause_after_s or not state.get("colony")
                or is_danger(state) or now - last_set < RAISE_COOLDOWN_S):
            return None
        return 1 if danger_at is not None and now - danger_at < CALM_RETURN_S else MAX_SPEED
    speed = state.get("time_speed")
    if not isinstance(speed, int) or speed < 1:
        return None
    if is_danger(state):
        return 1 if speed > 1 else None             # агент должен успеть среагировать
    if speed >= MAX_SPEED:
        return None
    if danger_at is not None and now - danger_at < CALM_RETURN_S:
        return None
    if now - last_set < RAISE_COOLDOWN_S:
        return None
    return MAX_SPEED
