"""Ошибки самой игры (ТЗ 11c, GT-5): мод держит журнал отладки закрытым, пока агент подключён, и отдаёт ошибки в `/state`
(поле `game_log`: счётчики и последние 20 строк). Здесь решается, какие строки новые и что записать в журнал агента."""

MAX_PER_REPORT = 3          # не больше стольких новых строк за один опрос состояния: лавина ошибок не должна засорять журнал
LINE_MAX = 300


def new_errors(game_log, seen: int) -> tuple[int, list[str]]:
    """По полю `game_log` состояния и числу уже учтённых ошибок вернуть (новое число учтённых, новые строки).
    Счётчик упал (игра перезапущена, журнал очищен): отсчёт начинается заново, а накопленные строки считаются новыми."""
    if not isinstance(game_log, dict):
        return seen, []
    try:
        total = int(game_log.get("errors") or 0)
    except (TypeError, ValueError):
        return seen, []
    lines = [str(x)[:LINE_MAX] for x in (game_log.get("last_errors") or []) if x]
    if total < seen:
        seen = 0
    fresh = total - seen
    if fresh <= 0:
        return total, []
    out = lines[-min(fresh, len(lines)):] if lines else []
    if len(out) > MAX_PER_REPORT:
        out = out[-MAX_PER_REPORT:]
    return total, out
