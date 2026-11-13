"""Расход Claude: как часто работают её игровые контуры (ход, планирование, цели, рефлексия, самоулучшение).

Подписка Claude Code имеет лимит сессии, а игровые контуры работали почти непрерывно (до 35 запусков в час,
каждый тянет контекст и читает состояние колонии). Режим в настройках («Расход Claude») задаёт темп.
Срочное не ограничивается ни в одном режиме: тревога, приказ игрока и бой идут сразу.

  turn_gap_s   : не чаще одного обычного хода за столько секунд;
  turn_idle_s  : ход случается и без перемен, если прошло столько секунд (иначе только при важных переменах);
  planning_s / longterm_s / reflection_s : паузы между фоновыми контурами;
  selfimp_s    : самоулучшение кода (0 = выключено).
"""
import settings

PRESETS = {
    "frugal":   {"turn_gap_s": 240, "turn_idle_s": 900, "planning_s": 1800, "longterm_s": 7200,
                 "reflection_s": 14400, "selfimp_s": 0},
    "normal":   {"turn_gap_s": 120, "turn_idle_s": 600, "planning_s": 900, "longterm_s": 3600,
                 "reflection_s": 7200, "selfimp_s": 0},
    "generous": {"turn_gap_s": 45, "turn_idle_s": 300, "planning_s": 480, "longterm_s": 1500,
                 "reflection_s": 2400, "selfimp_s": 7200},
}


def get() -> dict:
    return PRESETS.get(settings.load().get("claude_usage", "normal"), PRESETS["normal"])
