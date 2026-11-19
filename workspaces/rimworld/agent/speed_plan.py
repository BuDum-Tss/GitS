"""Автоскорость игры: в спокойное время держать максимальную скорость, при опасности снижать до обычной.

`plan_speed` чистая: по состоянию мода решает, какую скорость поставить (или None: ничего не делать). Применяет её controller
(команда мода `set_time_speed`). Скорости мода: 0 пауза, 1 обычная, 2 быстрая, 3 очень быстрая, 4 максимальная.
Опасность (`is_danger`): враги, бой, мобилизованные, упавшие, кровоточащие, тяжело раненные колонисты, срыв. Пока она есть,
скорость не повышаем и паузу не снимаем. Паузу, которую агент поставил сам (свежая метка), и паузу, пока агент активно
вызывает инструменты, тоже не снимаем (agent_activity)."""
import statediff

MODES = ("off", "max")
DEFAULT_MODE = "max"
MAX_SPEED = 4
RAISE_COOLDOWN_S = 20      # не чаще раза в столько секунд повышаем скорость (игра сама сбрасывает её на событиях)
AUTO_UNPAUSE_S = 90        # забытая пауза: снимаем, когда она идёт подряд не меньше стольких секунд (настройка auto_unpause_s, 0 = выкл.)
CALM_RETURN_S = 30         # после опасности максимум возвращаем, когда спокойно не меньше этого
LOW_HEALTH_PCT = 0.5       # колонист со здоровьем ниже этого (доля) тяжело ранен: опасность
BLEED_MIN = 0.01           # кровотечение выше этого (BleedRateTotal мода) считается кровотечением

REASON_DANGER = "опасность"
REASON_AGENT_PAUSE = "пауза агента"
REASON_AGENT_ACTIVE = "агент активен"


def normalize(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in MODES else DEFAULT_MODE


def _pawn_in_trouble(p: dict) -> bool:
    if p.get("is_drafted") or p.get("current_job") in statediff._FIGHT_JOBS:
        return True
    if p.get("is_prisoner"):                        # пленные не наши колонисты: их раны скорость не касаются
        return False
    if p.get("is_downed") or (p.get("bleeding") or 0) > BLEED_MIN:
        return True
    if p.get("in_mental_break") or p.get("mental_state"):      # психический срыв, паника (мод пока поле не отдаёт: на вырост)
        return True
    hp = p.get("health_pct")
    return isinstance(hp, (int, float)) and hp < LOW_HEALTH_PCT


def is_danger(state: dict) -> bool:
    """Опасность: угроза на карте, бой, мобилизованный, упавший, кровоточащий или тяжело раненный колонист, срыв."""
    if state.get("threats"):
        return True
    return any(_pawn_in_trouble(p) for p in (state.get("colony") or {}).get("pawns") or [])


def defer_reason(state: dict, now: float, agent_pause_at: float | None = None, agent_hold_s: float = 600.0,
                 tool_at: float | None = None, agent_active_s: float = 90.0) -> str | None:
    """Почему паузу пока не снимать: опасность, пауза агента (свежая метка) или агент активен (недавний вызов инструмента)."""
    if is_danger(state):
        return REASON_DANGER
    if agent_pause_at is not None and now - agent_pause_at < agent_hold_s:
        return REASON_AGENT_PAUSE
    if agent_active_s > 0 and tool_at is not None and now - tool_at < agent_active_s:
        return REASON_AGENT_ACTIVE
    return None


def decide(state: dict, mode: str, now: float, last_set: float, paused_by_planning: bool = False,
           danger_at: float | None = None, paused_since: float | None = None, unpause_after_s: float = AUTO_UNPAUSE_S,
           agent_pause_at: float | None = None, agent_hold_s: float = 600.0, tool_at: float | None = None,
           agent_active_s: float = 90.0) -> tuple[int | None, str | None]:
    """(скорость или None, причина отложенного снятия паузы или None). Причина есть, только когда пауза уже «забыта» по времени,
    но снять её не дают опасность, метка паузы агента или его активность."""
    if normalize(mode) != "max" or paused_by_planning:
        return None, None
    if state.get("paused"):
        if (unpause_after_s <= 0 or paused_since is None or now - paused_since < unpause_after_s or not state.get("colony")
                or now - last_set < RAISE_COOLDOWN_S):
            return None, None
        reason = defer_reason(state, now, agent_pause_at, agent_hold_s, tool_at, agent_active_s)
        if reason:
            return None, reason
        return (1 if danger_at is not None and now - danger_at < CALM_RETURN_S else MAX_SPEED), None
    speed = state.get("time_speed")
    if not isinstance(speed, int) or speed < 1:
        return None, None
    if is_danger(state):
        return (1 if speed > 1 else None), None     # агент должен успеть среагировать
    if speed >= MAX_SPEED or (agent_active_s > 0 and tool_at is not None and now - tool_at < agent_active_s):
        return None, None                           # агент думает, а не забыл: скорость не повышаем
    if danger_at is not None and now - danger_at < CALM_RETURN_S:
        return None, None
    if now - last_set < RAISE_COOLDOWN_S:
        return None, None
    return MAX_SPEED, None


def plan_speed(state: dict, mode: str, now: float, last_set: float, paused_by_planning: bool = False,
               danger_at: float | None = None, paused_since: float | None = None, unpause_after_s: float = AUTO_UNPAUSE_S,
               agent_pause_at: float | None = None, agent_hold_s: float = 600.0, tool_at: float | None = None,
               agent_active_s: float = 90.0) -> int | None:
    """Какую скорость поставить сейчас. `last_set` время прошлой нашей команды; `danger_at` когда в последний раз была опасность
    (None: не было). Паузу планирования не трогаем. Обычную паузу снимаем только как забытую: она идёт подряд (`paused_since`)
    не меньше `unpause_after_s` (0: не снимать), опасности нет, игра в колонии, это не свежая пауза агента (`agent_pause_at`,
    `agent_hold_s`) и агент не вызывал инструменты последние `agent_active_s` секунд (`tool_at`). Пока агент активен, и
    скорость не повышаем. Команда `set_time_speed` с ненулевой скоростью сама выводит игру из паузы."""
    return decide(state, mode, now, last_set, paused_by_planning, danger_at, paused_since, unpause_after_s,
                  agent_pause_at, agent_hold_s, tool_at, agent_active_s)[0]
